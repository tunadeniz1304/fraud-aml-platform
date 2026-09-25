"""Rule studio + policy API (P0.5): rule CRUD, backtest, thresholds, models.

* reads — ``analist``; rule changes — ``kidemli_analist``; policy thresholds —
  ``admin``. Changes are maker-checker: the endpoint stores an approval request
  (HTTP 202) and a second user applies it (``app.cases.governance``). Every
  applied change is versioned and written to the audit chain, and the live
  engine's ruleset is swapped atomically (hot reload, no restart).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_pipeline
from app.cases.service import CaseError
from app.config import get_settings
from app.features.definitions import EXTERNAL_FEATURES, FEATURES
from app.ml.registry import ModelRegistry
from app.scoring import rule_store
from app.scoring.policy import PolicyError, Thresholds
from app.scoring.rules import (
    ACTIONS,
    SEVERITIES,
    BacktestCancelled,
    RuleDef,
    RuleError,
    backtest,
    with_overrides,
)
from app.security.auth import Principal
from app.security.deps import forbid_break_glass_maker, require_role

router = APIRouter(prefix="/api", tags=["rules"])
analyst_only = Depends(require_role("analist"))


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    name: str = Field(min_length=1, max_length=160)
    when: str = Field(min_length=1, max_length=500)
    score: float = Field(gt=0, le=1)
    description: str = Field(default="", max_length=1000)
    reason_code: str = Field(default="", max_length=64)
    reason_template: str = Field(default="", max_length=300)
    action_hint: Literal["ALLOW", "STEP_UP", "HOLD", "BLOCK"] | None = None
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    enabled: bool = True
    tags: list[str] = Field(default_factory=list, max_length=10)


class SimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str | None = Field(default=None, max_length=500)
    score: float | None = Field(default=None, gt=0, le=1)
    source: Literal["auto", "db", "synthetic"] = "auto"
    limit: int = Field(default=20_000, ge=1, le=50_000)


class DraftSimulateIn(RuleIn):
    source: Literal["auto", "db", "synthetic"] = "auto"
    limit: int = Field(default=20_000, ge=1, le=50_000)


class ThresholdsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_up: float = Field(gt=0, lt=1)
    hold: float = Field(gt=0, lt=1)
    block: float = Field(gt=0, le=1)


def _engine() -> Any:
    return require_pipeline().analyst.engine


async def _request(
    kind: str, target: str, payload: dict[str, Any], principal: Principal, note: str, message: str
) -> JSONResponse:
    """Decision-logic changes are maker-checker: the request is stored and a
    second user applies it by approving (``/api/approvals/{id}/approve``)."""
    forbid_break_glass_maker(principal)
    try:
        approval = await require_pipeline().cases.request_approval(
            kind, target, payload, principal.username, note
        )
    except CaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return JSONResponse(
        status_code=http_status.HTTP_202_ACCEPTED,
        content=jsonable_encoder({"message": message, "approval": approval}),
    )


@router.get("/rules", dependencies=[analyst_only])
async def list_rules() -> dict[str, Any]:
    pipeline = require_pipeline()
    async with pipeline.db.session() as session:
        definitions = await rule_store.load_rules(session)
    return {
        "version": pipeline.analyst.engine.ruleset.version,
        "rules": [d.to_dict() for d in definitions],
        "fields": {n: FEATURES[n].description for n in FEATURES} | dict(EXTERNAL_FEATURES),
        "actions": list(ACTIONS),
        "severities": list(SEVERITIES),
    }


@router.get("/rules/{rule_id}", dependencies=[analyst_only])
async def get_rule(rule_id: str) -> dict[str, Any]:
    pipeline = require_pipeline()
    async with pipeline.db.session() as session:
        rule = await rule_store.get_rule(session, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Kural bulunamadı")
        history = await rule_store.rule_history(session, rule_id)
    return {"rule": rule.to_dict(), "history": history}


async def _save(body: RuleIn, principal: Principal, *, create: bool) -> JSONResponse:
    pipeline = require_pipeline()
    try:
        definition = RuleDef.from_dict(body.model_dump())
        definition.validate()
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    async with pipeline.db.session() as session:
        current = await rule_store.get_rule(session, definition.id)
    if create and current is not None:
        raise HTTPException(status_code=409, detail="Bu kimlikte bir kural zaten var")
    if not create and current is None:
        raise HTTPException(status_code=404, detail="Kural bulunamadı")
    return await _request(
        "RULE_CHANGE",
        definition.id,
        {
            "op": "create" if create else "update",
            "definition": definition.to_dict(),
            "base_version": current.version if current is not None else None,
        },
        principal,
        f"Kural {'oluşturma' if create else 'güncelleme'} talebi: {definition.id}",
        "Kural değişikliği maker-checker onayı bekliyor",
    )


@router.post("/rules", status_code=http_status.HTTP_202_ACCEPTED, response_model=None)
async def create_rule(
    body: RuleIn, principal: Principal = Depends(require_role("kidemli_analist"))
) -> JSONResponse:
    return await _save(body, principal, create=True)


@router.put("/rules/{rule_id}", status_code=http_status.HTTP_202_ACCEPTED, response_model=None)
async def update_rule(
    rule_id: str, body: RuleIn, principal: Principal = Depends(require_role("kidemli_analist"))
) -> JSONResponse:
    if body.id != rule_id:
        raise HTTPException(status_code=422, detail="Gövdedeki kimlik yol ile uyuşmuyor")
    return await _save(body, principal, create=False)


@router.delete("/rules/{rule_id}", status_code=http_status.HTTP_202_ACCEPTED, response_model=None)
async def disable_rule(
    rule_id: str, principal: Principal = Depends(require_role("kidemli_analist"))
) -> JSONResponse:
    """Soft delete: the rule is disabled (history is immutable) once approved."""
    pipeline = require_pipeline()
    async with pipeline.db.session() as session:
        rule = await rule_store.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Kural bulunamadı")
    return await _request(
        "RULE_CHANGE",
        rule_id,
        {"op": "disable", "definition": rule.to_dict(), "base_version": rule.version},
        principal,
        f"Kural devre dışı bırakma talebi: {rule_id}",
        "Kural değişikliği maker-checker onayı bekliyor",
    )


async def _backtest_rows(source: str, limit: int) -> tuple[str, list[Any]]:
    pipeline = require_pipeline()
    settings = get_settings()
    limit = min(limit, settings.rule_backtest_max_rows)
    synthetic = settings.data_dir / "generated" / "backtest.jsonl"
    rows: list[Any] = []
    if source in ("auto", "db"):
        async with pipeline.db.session() as session:
            rows = await rule_store.db_backtest_rows(session, limit)
        labelled = sum(1 for _, label in rows if label is not None)
        if source == "db" or labelled >= 50:
            return "db", rows
    synth = await asyncio.to_thread(rule_store.synthetic_backtest_rows, synthetic, limit)
    if synth or source == "synthetic":
        return "synthetic", synth
    return "db", rows


async def _run_backtest(draft: RuleDef, source: str, limit: int) -> dict[str, Any]:
    """Replay off the event loop, within the row cap and the time budget.

    M6: the worker thread cannot be killed, so it gets a ``stop`` event that
    is set as soon as this request gives up (timeout or cancellation); the
    replay checks it and ends instead of burning a thread-pool slot.
    """
    rows_source, rows = await _backtest_rows(source, limit)
    stop = threading.Event()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(backtest, draft, rows, source=rows_source, stop=stop),
            timeout=get_settings().rule_backtest_timeout_s,
        )
    except (TimeoutError, BacktestCancelled):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Simülasyon süre sınırını aştı — daha az satırla (limit) deneyin",
        ) from None
    finally:
        stop.set()
    return result.as_dict() | {"when": draft.when}


@router.post("/rules/{rule_id}/simulate", dependencies=[analyst_only])
async def simulate_rule(rule_id: str, body: SimulateIn | None = None) -> dict[str, Any]:
    """Backtest a rule (optionally a what-if variant) on historical decisions."""
    body = body or SimulateIn()
    pipeline = require_pipeline()
    async with pipeline.db.session() as session:
        rule = await rule_store.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Kural bulunamadı")
    try:
        draft = with_overrides(rule, {"when": body.when, "score": body.score, "enabled": True})
        draft.validate()
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await _run_backtest(draft, body.source, body.limit)


@router.post("/rules/simulate", dependencies=[analyst_only])
async def simulate_draft(body: DraftSimulateIn) -> dict[str, Any]:
    try:
        draft = RuleDef.from_dict(body.model_dump(exclude={"source", "limit"}))
        draft.validate()
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await _run_backtest(draft, body.source, body.limit)


# --- policy -------------------------------------------------------------------------------
@router.get("/policy", dependencies=[analyst_only])
async def get_policy() -> dict[str, Any]:
    engine = _engine()
    return {
        "thresholds": engine.policy.thresholds.as_dict(),
        "signal_weights": engine.policy.weights,
        "hold_cooling_off_minutes": get_settings().hold_cooling_off_minutes,
        "ruleset_version": engine.ruleset.version,
        "model_version": engine.model.version if engine.model else None,
        "challenger_version": engine.challenger.version if engine.challenger else None,
        "stacker": engine.policy.stacker.to_dict() if engine.policy.stacker else None,
    }


@router.put("/policy/thresholds", status_code=http_status.HTTP_202_ACCEPTED, response_model=None)
async def set_thresholds(
    body: ThresholdsIn, principal: Principal = Depends(require_role("admin"))
) -> JSONResponse:
    try:
        Thresholds(body.step_up, body.hold, body.block)
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await _request(
        "POLICY_THRESHOLDS",
        "thresholds",
        body.model_dump(),
        principal,
        "Politika eşiği değişikliği talebi",
        "Eşik değişikliği maker-checker onayı bekliyor",
    )


# --- models (read-only here; champion/challenger management in F6) ------------------------
@router.get("/models", dependencies=[analyst_only])
async def list_models() -> dict[str, Any]:
    registry = ModelRegistry(get_settings().resolved_models_dir)
    data = registry.read()
    return {
        "champion": data["champion"],
        "challenger": data["challenger"],
        "models": registry.models(),
    }
