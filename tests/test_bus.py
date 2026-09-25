"""Event bus tests (P0.2): retries, dead-letter queue, idempotency, partitioned
ordering, backpressure — for the in-memory bus and Redis Streams (fakeredis)."""

from __future__ import annotations

import asyncio
from collections import Counter

import fakeredis
import pytest

from app.bus.memory import InMemoryBus
from app.bus.redis_streams import RedisStreamsBus


# --- in-memory: direct mode ------------------------------------------------------------
async def test_retry_then_success_does_not_dead_letter():
    bus = InMemoryBus(max_retries=2)
    attempts = Counter()

    async def flaky(payload):
        attempts[payload["id"]] += 1
        if attempts[payload["id"]] < 2:
            raise RuntimeError("geçici hata")

    bus.subscribe("t", flaky)
    await bus.publish("t", {"id": "a"}, key="a")
    assert attempts["a"] == 2 and not bus.dlq


async def test_permanent_failure_goes_to_dlq_and_others_still_receive():
    bus = InMemoryBus(max_retries=2)
    received = []

    def boom(payload):
        raise ValueError("kalıcı hata")

    bus.subscribe("t", boom)
    bus.subscribe("t", received.append)
    await bus.publish("t", {"id": 1}, key="k1")
    assert received == [{"id": 1}]
    assert len(bus.dlq) == 1
    letter = bus.dlq_entries()[0]
    assert letter["attempts"] == 3 and "kalıcı hata" in letter["error"]
    assert letter["key"] == "k1" and letter["topic"] == "t"


async def test_10k_replay_no_loss_no_duplicates():
    bus = InMemoryBus()
    seen = Counter()
    bus.subscribe("tx", lambda p: seen.update([p["id"]]))
    for _ in range(2):  # original + full replay
        for i in range(10_000):
            await bus.publish("tx", {"id": i}, key=f"TX-{i}")
    assert len(seen) == 10_000 and set(seen.values()) == {1}
    assert bus.duplicates == 10_000


async def test_messages_without_key_are_not_deduplicated():
    bus = InMemoryBus()
    seen = []
    bus.subscribe("t", seen.append)
    await bus.publish("t", {"x": 1})
    await bus.publish("t", {"x": 1})
    assert len(seen) == 2


async def test_dedupe_memory_is_bounded():
    bus = InMemoryBus(dedupe_size=100)
    bus.subscribe("t", lambda p: None)
    for i in range(1_000):
        await bus.publish("t", {}, key=str(i))
    assert len(bus._done) == 100


# --- in-memory: partitioned mode ------------------------------------------------------------
async def test_partitions_preserve_per_key_order_with_concurrency():
    bus = InMemoryBus(partitions=4, queue_size=16)
    order: dict[str, list[int]] = {}

    async def handler(payload):
        await asyncio.sleep(0)  # yield -> real interleaving across partitions
        order.setdefault(payload["cust"], []).append(payload["seq"])

    bus.subscribe("t", handler)
    await bus.start()
    for seq in range(50):
        for cust in ("A", "B", "C", "D", "E"):
            await bus.publish("t", {"cust": cust, "seq": seq}, key=f"{cust}-{seq}", partition=cust)
    await bus.drain()
    await bus.stop()
    assert all(v == list(range(50)) for v in order.values()) and len(order) == 5


async def test_backpressure_bounds_the_queue():
    bus = InMemoryBus(partitions=1, queue_size=5)
    peak = 0

    async def slow(payload):
        await asyncio.sleep(0.002)

    bus.subscribe("t", slow)
    await bus.start()
    for i in range(60):
        await bus.publish("t", {"i": i}, key="same")
        peak = max(peak, bus.backlog)
    await bus.stop()
    assert peak <= 5


async def test_nested_publish_from_worker_does_not_deadlock():
    bus = InMemoryBus(partitions=1, queue_size=1)
    done = []

    async def stage1(payload):
        await bus.publish("stage2", payload, key=f"s2-{payload['i']}")

    bus.subscribe("stage1", stage1)
    bus.subscribe("stage2", done.append)
    await bus.start()
    for i in range(20):
        await bus.publish("stage1", {"i": i}, key=f"k{i}")
    await asyncio.wait_for(bus.drain(), timeout=5)
    await bus.stop()
    assert len(done) == 20


# --- redis streams (fakeredis) ----------------------------------------------------------------
@pytest.fixture()
async def redis_bus():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bus = RedisStreamsBus(redis, block_ms=10, claim_idle_ms=5, max_retries=2, batch=200)
    yield bus
    await bus.stop()


async def test_redis_consumer_group_exactly_once_with_replay(redis_bus):
    seen = Counter()

    async def handler(payload):
        seen[payload["i"]] += 1

    redis_bus.subscribe("transaction.created", handler, group="pipeline")
    await redis_bus.start()
    for _ in range(2):
        for i in range(1_000):
            await redis_bus.publish("transaction.created", {"i": i}, key=f"TX-{i}")
    await redis_bus.drain(20)
    assert len(seen) == 1_000 and set(seen.values()) == {1}
    assert redis_bus.duplicates == 1_000
    assert await redis_bus.pending() == 0


async def test_redis_retry_then_success(redis_bus):
    attempts = Counter()

    async def flaky(payload):
        attempts["x"] += 1
        if attempts["x"] < 2:
            raise RuntimeError("geçici")

    redis_bus.subscribe("t", flaky, group="g")
    await redis_bus.start()
    await redis_bus.publish("t", {"v": 1}, key="only")
    await redis_bus.drain(10)
    assert attempts["x"] == 2 and await redis_bus.dlq_size() == 0


async def test_redis_poison_message_lands_in_dlq(redis_bus):
    async def poison(payload):
        raise ValueError("bozuk mesaj")

    redis_bus.subscribe("t", poison, group="g")
    await redis_bus.start()
    await redis_bus.publish("t", {"v": 1}, key="bad-1")
    await redis_bus.drain(10)
    assert await redis_bus.dlq_size() == 1
    entry = (await redis_bus.dlq_entries())[0]
    assert entry["key"] == "bad-1" and entry["attempts"] == "3"
    assert entry["payload"] == {"v": 1} and "bozuk mesaj" in entry["error"]


async def test_redis_two_groups_each_get_every_message(redis_bus):
    a, b = [], []
    redis_bus.subscribe("t", a.append, group="scorer")
    redis_bus.subscribe("t", b.append, group="auditor")
    await redis_bus.start()
    for i in range(10):
        await redis_bus.publish("t", {"i": i}, key=str(i))
    await redis_bus.drain(10)
    assert len(a) == len(b) == 10


async def test_l9_reused_key_with_another_payload_is_dead_lettered(redis_bus):
    """A processed key redelivered with a *different* payload is flagged in the
    DLQ (IdempotencyConflict), never silently acked as a duplicate."""
    seen = []
    redis_bus.subscribe("t", seen.append, group="g")
    await redis_bus.start()
    tx = {"transaction_id": "K-1", "amount": 10, "ts": "2026-09-26T12:00:00+03:00"}
    await redis_bus.publish("t", tx, key="K-1")
    await redis_bus.drain(10)
    # the same instant in another offset is the same payload -> plain duplicate
    await redis_bus.publish("t", {**tx, "ts": "2026-09-26T09:00:00Z"}, key="K-1")
    await redis_bus.drain(10)
    assert redis_bus.duplicates == 1 and await redis_bus.dlq_size() == 0
    await redis_bus.publish("t", {**tx, "amount": 99}, key="K-1")
    await redis_bus.drain(10)
    assert seen == [tx] and redis_bus.conflicts == 1
    entry = (await redis_bus.dlq_entries())[0]
    assert entry["key"] == "K-1" and "IdempotencyConflict" in entry["error"]
    assert entry["payload"]["amount"] == 99
    assert await redis_bus.pending() == 0


async def test_l9_legacy_done_marker_still_acks_duplicates(redis_bus):
    """Markers written before L9 hold ``"1"``: treated as a plain duplicate."""
    seen = []
    redis_bus.subscribe("t", seen.append, group="g")
    await redis_bus.redis.set(redis_bus._done_key("g", "OLD-1"), "1")
    await redis_bus.start()
    await redis_bus.publish("t", {"v": 2}, key="OLD-1")
    await redis_bus.drain(10)
    assert seen == [] and redis_bus.duplicates == 1 and await redis_bus.dlq_size() == 0
