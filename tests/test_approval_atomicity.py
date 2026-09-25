"""A5: a maker-checker approval applies all of its effects or none of them.

The status flip, the handler's DB writes (rule row, ``runtime_config``
publish), and the audit entries are one transaction; the local engine only
changes after the commit. A failed approval stays ``BEKLIYOR`` and a retry
succeeds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.cases.service import ApprovalUnavailableError, CaseError
from app.config import get_settings
from app.db.models import Approval, RuntimeConfig
from app.pipeline import Pipeline, build_pipeline
from app.scoring import rule_store
from app.services.config_sync import ConfigConflict


@pytest.fixture()
async def pipe(app_env, monkeypatch) -> AsyncIterator[Pipeline]:
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    p = await build_pipeline(get_settings())
    await p.start()
    try:
        yield p
    finally:
        await p.stop()
        get_settings.cache_clear()


async def _status(p: Pipeline, approval_id: int) -> str:
    async with p.db.session() as s:
        return (
            await s.execute(select(Approval.status).where(Approval.id == approval_id))
        ).scalar_one()


async def _config(p: Pipeline, key: str) -> Any:
    async with p.db.session() as s:
        return (
            await s.execute(select(RuntimeConfig.value).where(RuntimeConfig.key == key))
        ).scalar_one_or_none()


def _failing(exc: BaseException):
    async def publish_in(*_a: Any, **_k: Any) -> int:
        raise exc

    return publish_in


async def test_threshold_approval_failure_leaves_nothing_applied_and_is_retryable(pipe):
    before = pipe.engine.policy.thresholds.as_dict()
    a = await pipe.cases.request_approval(
        "POLICY_THRESHOLDS", "thresholds", {"step_up": 0.11, "hold": 0.22, "block": 0.33}, "maker1"
    )
    real = pipe.config.publish_in
    pipe.config.publish_in = _failing(RuntimeError("simulated publish failure"))
    with pytest.raises(RuntimeError):
        await pipe.cases.decide_approval(a["id"], approve=True, actor="checker1", role="admin")
    assert await _status(pipe, a["id"]) == "BEKLIYOR"
    assert pipe.engine.policy.thresholds.as_dict() == before
    assert await _config(pipe, "thresholds") is None

    pipe.config.publish_in = real
    done = await pipe.cases.decide_approval(a["id"], approve=True, actor="checker1", role="admin")
    assert done["status"] == "ONAYLANDI"
    assert await _status(pipe, a["id"]) == "ONAYLANDI"
    assert pipe.engine.policy.thresholds.as_dict()["hold"] == pytest.approx(0.22)
    assert (await _config(pipe, "thresholds"))["block"] == pytest.approx(0.33)


async def test_rule_change_failure_does_not_save_the_rule_and_retry_succeeds(pipe):
    async with pipe.db.session() as s:
        rule = (await rule_store.load_rules(s))[0]
    payload = {"op": "disable", "definition": rule.to_dict(), "base_version": rule.version}
    r = await pipe.cases.request_approval("RULE_CHANGE", rule.id, payload, "maker1")
    real = pipe.config.publish_in
    ruleset_before = pipe.engine.ruleset.version
    pipe.config.publish_in = _failing(RuntimeError("simulated publish failure"))
    with pytest.raises(RuntimeError):
        await pipe.cases.decide_approval(r["id"], approve=True, actor="checker1", role="admin")
    async with pipe.db.session() as s:
        stored = await rule_store.get_rule(s, rule.id)
    assert stored is not None and stored.version == rule.version and stored.enabled
    assert await _status(pipe, r["id"]) == "BEKLIYOR"
    assert pipe.engine.ruleset.version == ruleset_before

    pipe.config.publish_in = real
    await pipe.cases.decide_approval(r["id"], approve=True, actor="checker2", role="admin")
    async with pipe.db.session() as s:
        stored = await rule_store.get_rule(s, rule.id)
    assert stored is not None and not stored.enabled
    assert await _status(pipe, r["id"]) == "ONAYLANDI"
    assert pipe.engine.ruleset.version != ruleset_before


async def test_concurrent_config_change_maps_to_conflict(pipe):
    a = await pipe.cases.request_approval(
        "POLICY_THRESHOLDS", "thresholds", {"step_up": 0.1, "hold": 0.2, "block": 0.3}, "maker1"
    )
    pipe.config.publish_in = _failing(ConfigConflict("thresholds"))
    with pytest.raises(CaseError):
        await pipe.cases.decide_approval(a["id"], approve=True, actor="checker1", role="admin")
    assert await _status(pipe, a["id"]) == "BEKLIYOR"


async def test_database_outage_maps_to_unavailable(pipe):
    a = await pipe.cases.request_approval(
        "POLICY_THRESHOLDS", "thresholds", {"step_up": 0.1, "hold": 0.2, "block": 0.3}, "maker1"
    )
    pipe.config.publish_in = _failing(OperationalError("UPDATE", {}, Exception("down")))
    with pytest.raises(ApprovalUnavailableError):
        await pipe.cases.decide_approval(a["id"], approve=True, actor="checker1", role="admin")
    assert await _status(pipe, a["id"]) == "BEKLIYOR"


async def test_promotion_rollback_restores_the_registry(pipe, monkeypatch, tmp_path):
    import shutil

    from app.config import BASE_DIR
    from app.ml.registry import ModelRegistry

    models = tmp_path / "models"
    shutil.copytree(BASE_DIR / "models", models)
    monkeypatch.setattr(pipe.settings, "models_dir", models, raising=False)
    registry = ModelRegistry(models)
    before = registry.read()
    challenger = before["challenger"]
    a = await pipe.cases.request_approval("MODEL_PROMOTE", challenger, {}, "maker1")

    async def boom(*_a: Any, **_k: Any) -> None:
        raise OperationalError("INSERT", {}, Exception("audit write failed"))

    monkeypatch.setattr(pipe.writer.chain, "append_many", boom)
    with pytest.raises(ApprovalUnavailableError):
        await pipe.cases.decide_approval(a["id"], approve=True, actor="checker1", role="admin")
    assert registry.read()["champion"] == before["champion"]
    assert await _status(pipe, a["id"]) == "BEKLIYOR"
    assert await _config(pipe, "models") is None
