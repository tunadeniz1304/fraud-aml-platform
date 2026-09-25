"""H3 — state shared by several workers.

Two full pipelines (``uvicorn --workers 2``) share one fakeredis and one DB.
A block made by worker A is enforced by worker B; threshold, rule and
champion-model changes made on one worker reach the other, and persisted
thresholds survive a restart.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import fakeredis
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import RuntimeConfig
from app.pipeline import Pipeline, build_pipeline
from app.scoring import rule_store

CID = "CUST-0003"
TX: dict[str, Any] = {
    "transaction_id": "MW-1",
    "ts": "2026-09-22T11:00:00",
    "customer_id": CID,
    "amount": 120,
    "currency": "TRY",
    "device_id": "DEV-88CC-11",
    "location": "İzmir",
    "country": "TR",
    "channel": "mobile",
    "beneficiary_iban": "TR330006100519786457841326",
}


async def _until(check: Any, limit_s: float = 1.0) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    while not check():
        if loop.time() - started > limit_s:
            raise TimeoutError(f"not propagated within {limit_s} s")
        await asyncio.sleep(0.01)
    return loop.time() - started


@pytest.fixture()
def mw_env(app_env, monkeypatch):
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("ACCOUNT_STATUS_POLL_MS", "100")
    monkeypatch.setenv("CONFIG_POLL_MS", "100")
    get_settings.cache_clear()
    yield app_env
    get_settings.cache_clear()


@pytest.fixture()
async def workers(mw_env) -> AsyncIterator[tuple[Pipeline, Pipeline, Any]]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    a = await build_pipeline(get_settings(), redis=redis)
    b = await build_pipeline(get_settings(), redis=redis)
    await a.start()
    await b.start()
    try:
        yield a, b, redis
    finally:
        for p in (a, b):
            with_redis, p.redis = p.redis, None  # shared client: close once
            await p.stop()
            p.redis = with_redis
        await redis.aclose()


async def test_block_on_one_worker_is_enforced_by_the_other(workers):
    a, b, _ = workers
    await a.accounts.set_by_analyst(CID, "BLOKE", actor="analyst", reason="fraud")
    waited = await _until(lambda: b.accounts.get_status(CID) == "BLOKE")
    assert waited < 1.0
    result = await b.ingest(dict(TX))
    assert result is not None and result["decision"] == "BLOCK"


async def test_system_escalation_propagates_to_the_other_worker(workers):
    a, b, _ = workers
    await a.accounts.escalate(CID, "BLOKE", reason="score")
    await a.writer.flush()
    await _until(lambda: b.accounts.get_status(CID) == "BLOKE")
    # B's stale-cache escalation cannot undo it (M10 CAS)
    await b.accounts.escalate(CID, "INCELENIYOR", reason="late")
    await b.writer.flush()
    assert (await a.accounts.get_account(CID) or {})["hesap_durumu"] == "BLOKE"


async def test_threshold_change_propagates_and_survives_a_restart(workers, mw_env):
    a, b, redis = workers
    before = b.engine.policy.thresholds.as_dict()
    target = {"step_up": 0.21, "hold": 0.52, "block": 0.83}
    assert target != before
    await a.set_thresholds(**target, actor="admin")
    await _until(lambda: b.engine.policy.thresholds.as_dict() == target)
    await a.set_thresholds(0.22, 0.53, 0.84, actor="admin")
    await _until(lambda: b.engine.policy.thresholds.step_up == 0.22)
    async with a.db.session() as session:
        row = (
            await session.execute(select(RuntimeConfig).where(RuntimeConfig.key == "thresholds"))
        ).scalar_one()
    assert row.version == 2 and row.updated_by == "admin"

    # a restarted worker starts from the persisted thresholds, not the defaults
    c = await build_pipeline(get_settings(), redis=redis)
    assert c.engine.policy.thresholds.step_up != 0.22
    await c.start()
    try:
        assert c.engine.policy.thresholds.as_dict() == {
            "step_up": 0.22,
            "hold": 0.53,
            "block": 0.84,
        }
    finally:
        c.redis = None
        await c.stop()


async def test_invalid_thresholds_are_neither_applied_nor_published(workers):
    a, _, _ = workers
    before = a.engine.policy.thresholds.as_dict()
    with pytest.raises(ValueError):
        await a.set_thresholds(0.9, 0.5, 0.8, actor="admin")
    assert a.engine.policy.thresholds.as_dict() == before
    assert "thresholds" not in await a.config.read()


async def test_rule_change_reaches_the_other_worker(workers):
    a, b, _ = workers
    rule = b.engine.ruleset.rules[0]
    async with a.db.transaction() as session:
        stored = await rule_store.get_rule(session, rule.id)
        assert stored is not None
        await rule_store.save_rule(session, dataclasses.replace(stored, enabled=False), "u")
    version = await a.reload_rules(actor="u")
    await _until(lambda: b.engine.ruleset.version == version)
    assert rule.id not in {r.id for r in b.engine.ruleset.rules}


async def test_champion_reload_reaches_the_other_worker(workers, monkeypatch):
    a, b, _ = workers
    reloads: list[bool] = []
    original = b.reload_models

    async def spy(*, publish: bool = True) -> dict[str, str | None]:
        reloads.append(publish)
        return await original(publish=publish)

    monkeypatch.setattr(b, "reload_models", spy)
    loaded = await a.reload_models()
    await _until(lambda: reloads == [False])
    assert b.engine.model is None or b.engine.model.version == loaded["champion"]
