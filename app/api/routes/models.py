"""Model governance API (P1.6): champion/challenger, promotion, drift, active learning."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.api.deps import require_pipeline
from app.cases.service import CaseError
from app.config import get_settings
from app.db.models import Decision, Label
from app.ml import metrics as M
from app.ml.registry import ModelRegistry, RegistryError
from app.security.auth import Principal
from app.security.deps import require_role

router = APIRouter(prefix="/api/models", tags=["models"])
analyst = Depends(require_role("analist"))
senior = Depends(require_role("kidemli_analist"))


def _registry() -> ModelRegistry:
    return ModelRegistry(get_settings().resolved_models_dir)


@router.get("/compare", dependencies=[analyst])
async def compare(limit: int = Query(20_000, ge=10, le=200_000)) -> dict[str, Any]:
    """Offline metrics + online shadow comparison (and labelled outcomes if any)."""
    pipeline = require_pipeline()
    registry = _registry()
    data = registry.read()
    thresholds = pipeline.engine.policy.thresholds
    async with pipeline.db.session() as session:
        rows = (
            await session.execute(
                select(Decision, Label.label)
                .outerjoin(Label, Label.transaction_id == Decision.transaction_id)
                .where(Decision.challenger_score.is_not(None))
                .order_by(Decision.id.desc())
                .limit(limit)
            )
        ).all()
    champ = [float((d.components or {}).get("stacked") or 0.0) for d, _ in rows]
    chall = [float(d.challenger_score or 0.0) for d, _ in rows]
    online: dict[str, Any] = {"shadow_decisions": len(rows)}
    if rows:
        flag = thresholds.step_up
        agree = sum((a >= flag) == (b >= flag) for a, b in zip(champ, chall, strict=True))
        online.update(
            agreement=round(agree / len(rows), 4),
            champion_alert_rate=round(sum(a >= thresholds.hold for a in champ) / len(rows), 4),
            challenger_alert_rate=round(sum(b >= thresholds.hold for b in chall) / len(rows), 4),
        )
        labelled = [
            (y, a, b, d) for (d, y), a, b in zip(rows, champ, chall, strict=True) if y is not None
        ]
        if len({y for y, *_ in labelled}) == 2:
            ys = [y for y, *_ in labelled]
            fp_cost = get_settings().fp_cost_try

            def cost(scores: list[float]) -> float:
                total = 0.0
                for (y, _, _, d), s in zip(labelled, scores, strict=True):
                    flagged = s >= thresholds.hold
                    if flagged and not y:
                        total += fp_cost
                    elif not flagged and y:
                        total += float((d.features or {}).get("amount_try") or 0.0)
                return round(total, 2)

            a_scores = [a for _, a, _, _ in labelled]
            b_scores = [b for _, _, b, _ in labelled]
            online["labelled"] = {
                "n": len(labelled),
                "champion_pr_auc": round(M.pr_auc(ys, a_scores), 4),
                "challenger_pr_auc": round(M.pr_auc(ys, b_scores), 4),
                "champion_cost_try": cost(a_scores),
                "challenger_cost_try": cost(b_scores),
            }
    offline = {
        role: data["models"].get(data[role], {}).get("metrics") if data.get(role) else None
        for role in ("champion", "challenger")
    }
    return {
        "champion": data.get("champion"),
        "challenger": data.get("challenger"),
        "offline": offline,
        "online": online,
    }


@router.post("/{version}/promote", status_code=202)
async def request_promotion(
    version: str, principal: Principal = Depends(require_role("admin"))
) -> dict[str, Any]:
    """Promotion is maker-checker: another senior user approves it."""
    if version not in _registry().read()["models"]:
        raise HTTPException(status_code=404, detail="Model bulunamadı")
    try:
        return await require_pipeline().cases.request_approval(
            "MODEL_PROMOTE", version, {"version": version}, principal.username
        )
    except CaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.put("/{version}/challenger")
async def set_challenger(
    version: str, principal: Principal = Depends(require_role("admin"))
) -> dict[str, Any]:
    """Put a registered model into shadow scoring (no effect on decisions)."""
    try:
        data = _registry().set_status(version, "challenger")
    except RegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await require_pipeline().reload_models()
    return {
        "champion": data["champion"],
        "challenger": data["challenger"],
        "by": principal.username,
    }


@router.get("/drift", dependencies=[analyst])
async def drift() -> dict[str, Any]:
    monitor = require_pipeline().engine.drift
    if monitor is None:
        return {"observed": 0, "psi": {}, "watch": [], "alerts": [], "note": "model yok"}
    return monitor.status()


@router.get("/active-learning", dependencies=[analyst])
async def active_learning(
    limit: int = Query(20, ge=1, le=200),
    low: float = Query(0.4, ge=0, le=1),
    high: float = Query(0.6, ge=0, le=1),
) -> list[dict[str, Any]]:
    """Most uncertain, still unlabelled decisions — the best next labels."""
    pipeline = require_pipeline()
    async with pipeline.db.session() as session:
        rows = (
            await session.execute(
                select(Decision)
                .outerjoin(Label, Label.transaction_id == Decision.transaction_id)
                .where(Label.id.is_(None), Decision.risk_score.between(low, high))
                .order_by(Decision.id.desc())
                .limit(2000)
            )
        ).scalars()
        candidates = sorted(rows, key=lambda d: abs(d.risk_score - 0.5))[:limit]
    return [
        {
            "transaction_id": d.transaction_id,
            "customer_id": d.customer_id,
            "risk_score": d.risk_score,
            "decision": d.decision,
            "uncertainty": round(1 - 2 * abs(d.risk_score - 0.5), 4),
            "reason_codes": d.reason_codes[:3],
        }
        for d in candidates
    ]


@router.get("/validation", dependencies=[senior])
async def validation() -> dict[str, Any]:
    """Public-data validation results (``artifacts/validation/*/metrics.json``):
    PaySim replay, Elliptic graph, ULB, synthetic side by side and the
    champion selection evidence (docs/VALIDATION_REPORT.md)."""
    root = get_settings().resolved_validation_dir
    out: dict[str, Any] = {}
    for path in sorted(root.glob("*/metrics.json")):
        out[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    selection = root / "champion_selection.json"
    if selection.is_file():
        out["champion_selection"] = json.loads(selection.read_text(encoding="utf-8"))
    return out
