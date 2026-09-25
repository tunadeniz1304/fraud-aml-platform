"""Rule studio + policy API (P0.5): rule CRUD, backtest, thresholds, models.

* reads — ``analist``; rule changes — ``kidemli_analist``; policy thresholds —
  ``admin``. Every change is versioned and written to the audit chain, and the
  live engine's ruleset is swapped atomically (hot reload, no restart).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_pipeline
from app.config import get_settings
from app.db.audit import AuditEntry
from app.features.definitions import EXTERNAL_FEATURES, FEATURES
from app.ml.registry import ModelRegistry
from app.scoring import rule_store
from app.scoring.policy import PolicyError
from app.scoring.rules import ACTIONS, SEVERITIES, RuleDef, RuleError, backtest, with_overrides
from app.security.auth import Principal
from app.security.deps import require_role

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
    limit: int = Field(default=20_000, ge=1, le=200_000)


class DraftSimulateIn(RuleIn):
    source: Literal["auto", "db", "synthetic"] = "auto"
    limit: int = Field(default=20_000, ge=1, le=200_000)


class ThresholdsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_up: float = Field(gt=0, lt=1)
    hold: float = Field(gt=0, lt=1)
    block: float = Field(gt=0, le=1)


def _engine() -> Any:
    return require_pipeline().analyst.engine


async def _reload(pipeline: Any) -> str:
    # H3: rebuilds the rule set and bumps the shared generation for other workers
    return str(await pipeline.reload_rules())


async def _audit(pipeline: Any, event: str, entity: str, actor: str, reason: str, **payload: Any):
    await pipeline.writer.record_audit(
        AuditEntry(
            event_type=event,
            entity_type="rule" if event.startswith("RULE") else "policy",
            entity_id=entity,
            actor=actor,
            reason=reason,
            payload=payload,
        )
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


async def _save(body: RuleIn, principal: Principal, *, create: bool) -> dict[str, Any]:
    pipeline = require_pipeline()
    try:
        definition = RuleDef.from_dict(body.model_dump())
        definition.validate()
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    async with pipeline.db.transaction() as session:
        exists = await rule_store.get_rule(session, definition.id) is not None
        if create and exists:
            raise HTTPException(status_code=409, detail="Bu kimlikte bir kural zaten var")
        if not create and not exists:
            raise HTTPException(status_code=404, detail="Kural bulunamadı")
        stored = await rule_store.save_rule(session, definition, principal.username)
    version = await _reload(pipeline)
    await _audit(
        pipeline,
        "RULE_CREATE" if create else "RULE_UPDATE",
        stored.id,
        principal.username,
        f"Kural {'oluşturuldu' if create else 'güncellendi'}: {stored.id} v{stored.version}",
        definition=stored.to_dict(),
        ruleset_version=version,
    )
    return {"rule": stored.to_dict(), "ruleset_version": version}


@router.post("/rules", status_code=http_status.HTTP_201_CREATED)
async def create_rule(
    body: RuleIn, principal: Principal = Depends(require_role("kidemli_analist"))
) -> dict[str, Any]:
    return await _save(body, principal, create=True)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str, body: RuleIn, principal: Principal = Depends(require_role("kidemli_analist"))
) -> dict[str, Any]:
    if body.id != rule_id:
        raise HTTPException(status_code=422, detail="Gövdedeki kimlik yol ile uyuşmuyor")
    return await _save(body, principal, create=False)


@router.delete("/rules/{rule_id}")
async def disable_rule(
    rule_id: str, principal: Principal = Depends(require_role("kidemli_analist"))
) -> dict[str, Any]:
    """Soft delete: the rule is disabled (history is immutable)."""
    pipeline = require_pipeline()
    async with pipeline.db.transaction() as session:
        rule = await rule_store.get_rule(session, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Kural bulunamadı")
        stored = await rule_store.save_rule(
            session, RuleDef.from_dict({**rule.to_dict(), "enabled": False}), principal.username
        )
    version = await _reload(pipeline)
    await _audit(
        pipeline,
        "RULE_DISABLE",
        rule_id,
        principal.username,
        f"Kural devre dışı: {rule_id}",
        ruleset_version=version,
    )
    return {"rule": stored.to_dict(), "ruleset_version": version}


async def _backtest_rows(source: str, limit: int) -> tuple[str, list[Any]]:
    pipeline = require_pipeline()
    synthetic = get_settings().data_dir / "generated" / "backtest.jsonl"
    rows: list[Any] = []
    if source in ("auto", "db"):
        async with pipeline.db.session() as session:
            rows = await rule_store.db_backtest_rows(session, limit)
        labelled = sum(1 for _, label in rows if label is not None)
        if source == "db" or labelled >= 50:
            return "db", rows
    synth = rule_store.synthetic_backtest_rows(synthetic, limit)
    if synth or source == "synthetic":
        return "synthetic", synth
    return "db", rows


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
        source, rows = await _backtest_rows(body.source, body.limit)
        return backtest(draft, rows, source=source).as_dict() | {"when": draft.when}
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/rules/simulate", dependencies=[analyst_only])
async def simulate_draft(body: DraftSimulateIn) -> dict[str, Any]:
    try:
        draft = RuleDef.from_dict(body.model_dump(exclude={"source", "limit"}))
        source, rows = await _backtest_rows(body.source, body.limit)
        return backtest(draft, rows, source=source).as_dict() | {"when": draft.when}
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


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


@router.put("/policy/thresholds")
async def set_thresholds(
    body: ThresholdsIn, principal: Principal = Depends(require_role("admin"))
) -> dict[str, Any]:
    pipeline = require_pipeline()
    engine = pipeline.analyst.engine
    old = engine.policy.thresholds.as_dict()
    try:
        # H3: persisted with a version and picked up by every worker
        new = await pipeline.set_thresholds(
            body.step_up, body.hold, body.block, actor=principal.username
        )
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await _audit(
        pipeline,
        "POLICY_THRESHOLDS",
        "thresholds",
        principal.username,
        "Politika eşikleri değiştirildi",
        old=old,
        new=new.as_dict(),
    )
    return {"thresholds": new.as_dict()}


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
