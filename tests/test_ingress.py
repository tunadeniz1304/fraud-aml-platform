"""Redis Streams ingress/egress wiring of the pipeline (fakeredis) + worker."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from app.bus.redis_streams import RedisStreamsBus
from app.config import get_settings
from app.db import repository as repo
from app.db.models import Decision
from app.pipeline import build_pipeline

VALID = {
    "transaction_id": "ING-1",
    "ts": "2026-09-22T11:00:00",
    "customer_id": "CUST-0003",
    "amount": 420,
    "currency": "TRY",
    "device_id": "DEV-88CC-11",
    "location": "İzmir",
    "country": "TR",
    "channel": "mobile",
    "beneficiary_iban": "TR330006100519786457841326",
    "session": {"typing_cadence_ms": 190, "login_to_transfer_s": 45},
}


@pytest.fixture()
async def redis_pipeline(app_env, monkeypatch):
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("EVENT_BUS", "redis")
    monkeypatch.setenv("FEATURE_STORE", "redis")
    monkeypatch.setenv("BUS_CLAIM_IDLE_MS", "5")
    get_settings.cache_clear()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pipeline = await build_pipeline(get_settings(), redis=redis)
    pipeline.ingress.block_ms = 10  # type: ignore[union-attr]
    await pipeline.start()
    producer = RedisStreamsBus(redis, prefix=get_settings().redis_stream_prefix)
    yield pipeline, producer, redis
    await pipeline.stop()


async def test_ingress_scores_and_egress_publishes(redis_pipeline):
    pipeline, producer, redis = redis_pipeline
    await producer.publish("transaction.created", VALID, key="ING-1")
    await producer.publish("transaction.created", VALID, key="ING-1")  # duplicate delivery
    await pipeline.ingress.drain(10)
    await pipeline.drain()
    async with pipeline.db.session() as session:
        assert await repo.count(session, Decision) == 1
    assert pipeline.results["ING-1"]["features"]["login_to_transfer_s"] == 45
    egress = await redis.xrange("fraud:decision.made")
    assert len(egress) == 1 and '"ING-1"' in egress[0][1]["payload"]
    # Feature state lives in Redis (shared across processes).
    assert await redis.exists("fs:ev:CUST-0003")


async def test_invalid_ingress_payload_goes_to_dlq(redis_pipeline):
    pipeline, producer, _ = redis_pipeline
    bad = {**VALID, "transaction_id": "ING-BAD", "amount": -1, "hack": "x"}
    await producer.publish("transaction.created", bad, key="ING-BAD")
    await pipeline.ingress.drain(10)
    entries = await pipeline.ingress.dlq_entries()
    assert entries and entries[0]["key"] == "ING-BAD"
    assert "ValidationError" in entries[0]["error"]
    assert "ING-BAD" not in pipeline.results


async def test_worker_runs_jobs_and_stops(app_env, monkeypatch):
    from app.worker import run_worker

    monkeypatch.setenv("STREAM_MODE", "batch")
    get_settings.cache_clear()
    pipeline = await build_pipeline(get_settings())
    await pipeline.start()  # writes 9 audited decisions
    await pipeline.stop()
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if task.done():
            break
    stop.set()
    ctx = await asyncio.wait_for(task, 10)
    assert ctx.last_results["audit_verify"]["ok"] is True
    assert ctx.last_results["audit_verify"]["checked"] >= 9


async def test_persisted_duplicate_returns_stored_decision(app_env, monkeypatch):
    """After a restart (in-memory results lost) a replayed id returns the DB decision."""
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    pipeline = await build_pipeline(get_settings())
    await pipeline.start()
    try:
        first = await pipeline.ingest({**VALID, "transaction_id": "ING-DUP"})
        assert first is not None and first["decision"] in ("ALLOW", "STEP_UP", "HOLD", "BLOCK")
        await pipeline.writer.flush()
        pipeline.results.clear()  # simulate a process restart
        again = await pipeline.ingest({**VALID, "transaction_id": "ING-DUP"})
        assert again is not None and again["persisted_only"] and again["duplicate"]
        assert again["decision"] == first["decision"]
        assert again["risk_score"] == pytest.approx(first["risk_score"])
        from app.api.schemas import AnalyzedTransactionOut

        assert AnalyzedTransactionOut(**again).duplicate is True
        assert await pipeline.stored_result("NOPE") is None
    finally:
        await pipeline.stop()
