"""M12 — the per-customer feature lock fails closed and is never evicted while held.

* a Redis lease that cannot be obtained within ``FEATURE_LOCK_WAIT_MS`` raises
  :class:`FeatureLockTimeout` (it used to score without the lock); the pipeline
  turns it into a ``HOLD`` with reason ``FEATURE_LOCK_TIMEOUT`` and does not
  commit the event to the windows;
* the lease is renewed while held (a slow scoring step does not outlive it)
  and the commit is fenced on the lease token: a worker that lost the lease
  writes nothing;
* the per-process lock table is size-capped, but a held or awaited lock is
  never evicted (evicting it let a second coroutine run concurrently).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import fakeredis
import pytest

from app.config import get_settings
from app.features.store import (
    FeatureLockTimeout,
    MemoryFeatureStore,
    RedisFeatureStore,
    _LockTable,
)
from app.features.types import ProfileState, TxView
from app.pipeline import Pipeline, build_pipeline

CID = "CUST-0003"


@pytest.fixture()
async def redis() -> AsyncIterator[Any]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _store(redis: Any, wait_ms: int = 30) -> RedisFeatureStore:
    store = RedisFeatureStore(redis)
    store.lock_wait_s = wait_ms / 1000
    return store


# --- fail closed -----------------------------------------------------------------------
async def test_lease_timeout_fails_closed_and_keeps_the_foreign_lease(redis):
    store = _store(redis)
    await redis.set("fs:lock:C1", "other-worker", px=10_000)
    with pytest.raises(FeatureLockTimeout):
        async with store.customer_lock("C1"):
            pytest.fail("the body must not run without the lease")
    assert store.lock_timeouts == 1
    assert await redis.get("fs:lock:C1") == "other-worker"  # not released by us


async def test_lease_is_retried_until_the_holder_releases(redis):
    store = _store(redis, wait_ms=500)
    await redis.set("fs:lock:C1", "other-worker", px=10_000)

    async def release() -> None:
        await asyncio.sleep(0.03)
        await redis.delete("fs:lock:C1")

    releaser = asyncio.create_task(release())
    async with store.customer_lock("C1"):
        assert await redis.get("fs:lock:C1") not in (None, "other-worker")
    await releaser
    assert store.lock_timeouts == 0
    assert await redis.get("fs:lock:C1") is None  # owner-checked release


# --- renewal and fencing ---------------------------------------------------------------
async def test_lease_is_renewed_while_the_body_runs(redis):
    store = _store(redis)
    store.lock_ttl_ms = 60
    async with store.customer_lock("C1"):
        token = await redis.get("fs:lock:C1")
        await asyncio.sleep(0.2)  # > 3x the TTL
        assert await redis.get("fs:lock:C1") == token
    assert await redis.get("fs:lock:C1") is None


def _tx(tid: str) -> TxView:
    return TxView.from_payload(
        {"transaction_id": tid, "customer_id": "C1", "ts": "2026-09-25T10:00:00", "amount": 10}
    )


async def test_commit_is_fenced_on_the_lease_token(redis):
    store = _store(redis)
    async with store.customer_lock("C1"):
        await redis.set("fs:lock:C1", "other-worker")  # lease expired and was taken over
        await store.commit(_tx("F1"), ProfileState())
        assert store.lease_lost == 1
        assert await redis.zcard("fs:ev:C1") == 0
        assert await redis.get("fs:done:F1") is None  # a retry may still commit it
    assert await redis.get("fs:lock:C1") == "other-worker"  # foreign lease untouched
    async with store.customer_lock("C2"):
        pass
    await redis.delete("fs:lock:C1")
    async with store.customer_lock("C1"):
        await store.commit(_tx("F1"), ProfileState())
    assert await redis.zcard("fs:ev:C1") == 1


async def test_timeout_does_not_leak_the_local_lock(redis):
    store = _store(redis)
    await redis.set("fs:lock:C1", "other-worker", px=10_000)
    with pytest.raises(FeatureLockTimeout):
        async with store.customer_lock("C1"):
            pass
    await redis.delete("fs:lock:C1")
    async with store.customer_lock("C1"):  # the in-process lock was released
        pass
    assert store._local.in_use("C1") == 0


# --- lock table never evicts a held lock -----------------------------------------------
async def test_lock_table_never_evicts_a_held_lock():
    table = _LockTable(cap=2)
    order: list[str] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with table.hold("A"):
            entered.set()
            await release.wait()
            order.append("first-done")

    async def second() -> None:
        async with table.hold("A"):
            order.append("second-in")

    holder = asyncio.create_task(first())
    await entered.wait()
    for key in ("B", "C", "D", "E"):  # push A far past the cap
        async with table.hold(key):
            pass
    assert "A" in table and len(table) <= 2
    waiter = asyncio.create_task(second())
    await asyncio.sleep(0.01)
    assert order == []  # the second coroutine waits on the *same* lock
    release.set()
    await asyncio.gather(holder, waiter)
    assert order == ["first-done", "second-in"]


async def test_lock_table_grows_past_cap_only_while_locks_are_in_use():
    table = _LockTable(cap=1)
    gate = asyncio.Event()

    async def hold(key: str) -> None:
        async with table.hold(key):
            await gate.wait()

    tasks = [asyncio.create_task(hold(k)) for k in ("A", "B", "C")]
    await asyncio.sleep(0.01)
    assert len(table) == 3  # every lock is held: none may go
    gate.set()
    await asyncio.gather(*tasks)
    assert len(table) == 1


async def test_memory_store_lock_serialises_past_the_cap():
    store = MemoryFeatureStore(max_entities=1)
    inside = 0
    peak = 0

    async def worker(cid: str) -> None:
        nonlocal inside, peak
        async with store.customer_lock(cid):
            if cid == CID:
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.01)
                inside -= 1

    await asyncio.gather(*(worker(c) for c in [CID, "X1", CID, "X2", CID, "X3"]))
    assert peak == 1


# --- pipeline: HOLD with FEATURE_LOCK_TIMEOUT ------------------------------------------
@pytest.fixture()
def lock_env(app_env, monkeypatch):
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("FEATURE_STORE", "redis")
    monkeypatch.setenv("FEATURE_LOCK_WAIT_MS", "30")
    get_settings.cache_clear()
    yield app_env
    get_settings.cache_clear()


@pytest.fixture()
async def pipeline(lock_env, redis) -> AsyncIterator[Pipeline]:
    p = await build_pipeline(get_settings(), redis=redis)
    await p.start()
    try:
        yield p
    finally:
        p.redis = None  # the fixture closes the client
        await p.stop()


TX: dict[str, Any] = {
    "transaction_id": "LOCK-1",
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


async def test_pipeline_holds_when_the_feature_lock_times_out(pipeline, redis):
    await redis.set(f"fs:lock:{CID}", "other-worker", px=10_000)
    result = await pipeline.ingest(dict(TX))
    assert result is not None
    assert result["decision"] == "HOLD"
    assert result["lock_timeout"] is True and result["scored"] is False
    assert "FEATURE_LOCK_TIMEOUT" in {r["code"] for r in result["reason_codes"]}
    assert "FEATURE_LOCK_TIMEOUT" in result["policy"]["overrides"]
    # not committed to the windows: a retry after the lease frees is scored normally
    assert await redis.zcard(f"fs:ev:{CID}") == 0


async def test_pipeline_scores_normally_when_the_lease_is_free(pipeline, redis):
    result = await pipeline.ingest(dict(TX, transaction_id="LOCK-2"))
    assert result is not None
    assert result["scored"] is True and result["lock_timeout"] is False
    assert await redis.zcard(f"fs:ev:{CID}") == 1
