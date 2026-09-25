"""H4 chaos: the Redis ingress acknowledges a message only after its decision
is committed, so a worker crash between processing and commit ends in a
redelivery that is persisted exactly once."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import fakeredis
import pytest
from sqlalchemy import func, select

from app.bus.base import RetryLater
from app.bus.redis_streams import RedisStreamsBus
from app.config import get_settings
from app.db.models import AuditLog, Decision, Transaction
from app.pipeline import Pipeline, build_pipeline

TX: dict[str, Any] = {
    "transaction_id": "CHAOS-1",
    "ts": "2026-09-22T11:00:00",
    "customer_id": "CUST-0003",
    "amount": 420,
    "currency": "TRY",
    "device_id": "DEV-88CC-11",
    "location": "İzmir",
    "country": "TR",
    "channel": "mobile",
    "beneficiary_iban": "TR330006100519786457841326",
}


async def _until(check: Any, limit_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not await check():
        if loop.time() > deadline:
            raise TimeoutError("koşul gerçekleşmedi")
        await asyncio.sleep(0.02)


# --- bus level ---------------------------------------------------------------------
async def test_messages_are_acked_only_after_the_commit_barrier() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = asyncio.Event()
    barriers: list[int] = []

    async def barrier() -> None:
        barriers.append(1)
        await gate.wait()

    # fakeredis' blocking XREADGROUP delivers without returning: XAUTOCLAIM picks it up
    bus = RedisStreamsBus(redis, block_ms=10, claim_idle_ms=5, commit_barrier=barrier)
    handled: list[int] = []

    async def handler(payload: dict[str, Any]) -> None:
        handled.append(payload["i"])

    bus.subscribe("t", handler, group="g")
    for i in range(3):  # one read batch
        await bus.publish("t", {"i": i}, key=f"k{i}")
    await bus.start()
    try:

        async def waiting() -> bool:
            return len(handled) == 3 and bool(barriers)

        await _until(waiting)
        assert await bus.pending() == 3  # handled, not yet committed -> not acked
        assert not await redis.exists("fraud:done:g:k0")
        gate.set()
        await bus.drain(5)
        assert bus.processed == 3 and await bus.pending() == 0
    finally:
        await bus.stop()


async def test_retry_later_leaves_the_message_pending_without_a_failure() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bus = RedisStreamsBus(redis, block_ms=10, claim_idle_ms=20, max_retries=0)
    calls: list[int] = []

    async def handler(payload: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise RetryLater("başka worker işliyor")

    bus.subscribe("t", handler, group="g")
    await bus.start()
    try:
        await bus.publish("t", {"i": 1}, key="k1")
        await bus.drain(5)
    finally:
        await bus.stop()
    # max_retries=0 would have dead-lettered a failure at the first attempt
    assert len(calls) == 3 and bus.deferred == 2 and bus.processed == 1
    assert await bus.dlq_size() == 0


# --- pipeline chaos ------------------------------------------------------------------
@pytest.fixture()
def chaos_env(app_env, monkeypatch):
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("EVENT_BUS", "redis")
    monkeypatch.setenv("BUS_CLAIM_IDLE_MS", "300")
    monkeypatch.setenv("BUS_PROCESSING_LEASE_MS", "300")
    monkeypatch.setenv("IDEMPOTENCY_PENDING_TTL_S", "0.3")
    get_settings.cache_clear()
    return app_env


async def _crash(p: Pipeline) -> None:
    """Kill a worker without any graceful flush (process death)."""
    assert p.ingress is not None
    await p.ingress.stop()
    await p.idempotency.close()  # nobody refreshes its leases any more
    loops = [p._stream_task, p._vector_task, p._ring_task, p._drift_task]
    for task in [p.writer._task, *p._background, *loops]:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    p.writer._task = None
    await p.bus.stop()
    await p.effects.stop()


async def _rows(p: Pipeline, tx_id: str) -> tuple[int, int, int]:
    async with p.db.session() as session:
        txs = await session.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.id == tx_id)
        )
        decisions = await session.scalar(
            select(func.count()).select_from(Decision).where(Decision.transaction_id == tx_id)
        )
        audits = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.transaction_id == tx_id, AuditLog.event_type == "DECISION")
        )
    return int(txs or 0), int(decisions or 0), int(audits or 0)


async def _two_workers() -> tuple[Pipeline, Pipeline, RedisStreamsBus, Any]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    a = await build_pipeline(get_settings(), redis=redis)
    b = await build_pipeline(get_settings(), redis=redis)
    for p in (a, b):
        assert p.ingress is not None
        p.ingress.block_ms = 10
    producer = RedisStreamsBus(redis, prefix=get_settings().redis_stream_prefix)
    return a, b, producer, redis


async def test_crash_before_commit_is_redelivered_and_persisted_exactly_once(chaos_env):
    a, b, producer, _ = await _two_workers()
    hang = asyncio.Event()

    async def hung_commit(batch: Any) -> None:
        await hang.wait()  # the database never answers: worker A dies here

    a.writer._apply = hung_commit  # type: ignore[method-assign]
    await a.start()
    try:
        await producer.publish("transaction.created", TX, key="CHAOS-1")

        async def decided() -> bool:
            return "CHAOS-1" in a.results

        await _until(decided)
        await asyncio.sleep(0.05)
        assert a.ingress is not None and await a.ingress.pending() == 1  # not acked
        await _crash(a)
        assert await _rows(b, "CHAOS-1") == (0, 0, 0)  # lost with worker A ...

        await b.start()  # ... and redelivered to worker B
        assert b.ingress is not None
        await b.ingress.drain(15)
        await b.drain()
        assert "CHAOS-1" in b.results
        assert await _rows(b, "CHAOS-1") == (1, 1, 1)
        assert await b.ingress.pending() == 0 and await b.ingress.dlq_size() == 0
    finally:
        await b.stop()
        await a.db.dispose()


async def test_crash_after_commit_before_ack_is_not_scored_twice(chaos_env):
    a, b, producer, redis = await _two_workers()
    hang = asyncio.Event()
    committed = asyncio.Event()

    async def commit_then_die() -> None:
        await a.writer.barrier()
        committed.set()
        await hang.wait()  # the XACK never happens: worker A dies here

    assert a.ingress is not None
    a.ingress.commit_barrier = commit_then_die
    await a.start()
    try:
        await producer.publish("transaction.created", TX, key="CHAOS-1")
        await asyncio.wait_for(committed.wait(), 10)
        await a.wait_background()
        assert await _rows(a, "CHAOS-1") == (1, 1, 1)
        await _crash(a)
        # worst case: the Redis idempotency record is gone too
        await redis.delete("idem:CHAOS-1")

        await b.start()
        assert b.ingress is not None
        await b.ingress.drain(15)
        await b.drain()
        assert "CHAOS-1" not in b.results  # answered from the database, not re-scored
        assert await _rows(b, "CHAOS-1") == (1, 1, 1)
        assert await b.ingress.pending() == 0
    finally:
        await b.stop()
        await a.db.dispose()
