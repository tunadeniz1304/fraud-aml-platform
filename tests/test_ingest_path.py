"""V6 / B11: the ingest request path holds only score + decision + idempotency.

Case intake, live fan-out and egress run write-behind; a new transaction id
never queries the database; idempotency and SSE tickets work across workers
(processes sharing one Redis).
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis
import pytest

from app.config import get_settings
from app.idempotency import BloomFilter, IdempotencyIndex, ScalableBloom
from app.pipeline import build_pipeline
from app.security.auth import Principal
from app.security.tickets import TicketStore

TX: dict[str, Any] = {
    "transaction_id": "V6-1",
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


@pytest.fixture()
async def pipeline(app_env, monkeypatch):
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    p = await build_pipeline(get_settings())
    await p.start()
    yield p
    await p.stop()


def test_bloom_filter_has_no_false_negatives_and_few_false_positives() -> None:
    bloom = BloomFilter(10_000, error_rate=0.001)
    for i in range(10_000):
        bloom.add(f"TX-{i}")
    assert all(f"TX-{i}" in bloom for i in range(10_000))
    false_positives = sum(f"OTHER-{i}" in bloom for i in range(10_000))
    assert false_positives < 50  # 0.1 % target -> ~10 expected


async def test_new_transaction_does_not_query_the_database(pipeline, monkeypatch) -> None:
    calls: list[str] = []
    original = pipeline.stored_result

    async def counting(tx_id: str) -> dict[str, Any] | None:
        calls.append(tx_id)
        return await original(tx_id)

    monkeypatch.setattr(pipeline, "stored_result", counting)
    first = await pipeline.ingest(dict(TX))
    assert first is not None and first["decision"] in ("ALLOW", "STEP_UP", "HOLD", "BLOCK")
    assert calls == []  # Bloom negative: no idempotency query for a new id
    await pipeline.drain()
    pipeline.results.clear()  # simulate a restart
    again = await pipeline.ingest(dict(TX))
    assert calls == ["V6-1"]  # a possible replay is checked against the DB
    assert again is not None and again["duplicate"] and again["decision"] == first["decision"]


async def test_bloom_is_loaded_from_persisted_ids_on_start(app_env, monkeypatch) -> None:
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    p = await build_pipeline(get_settings())
    await p.start()
    try:
        await p.ingest(dict(TX))
    finally:
        await p.stop()
    restarted = await build_pipeline(get_settings())
    await restarted.start()
    try:
        assert restarted.idempotency.maybe_persisted("V6-1")
        again = await restarted.ingest(dict(TX))
        assert again is not None and again["persisted_only"] and again["duplicate"]
    finally:
        await restarted.stop()


async def test_response_does_not_wait_for_case_intake(pipeline, monkeypatch) -> None:
    gate = asyncio.Event()
    seen: list[str] = []

    async def slow_intake(event: dict[str, Any]) -> int | None:
        await gate.wait()  # e.g. a slow / locked database
        seen.append(str(event["transaction_id"]))
        return None

    monkeypatch.setattr(pipeline.cases, "on_decision", slow_intake)
    result = await asyncio.wait_for(pipeline.ingest(dict(TX)), timeout=5)
    assert result is not None and result["transaction_id"] == "V6-1"
    assert seen == [] and pipeline.effects.backlog + (not gate.is_set()) >= 1
    gate.set()
    await pipeline.wait_background()  # scenarios / tests can await the queue
    assert seen == ["V6-1"]


async def test_case_intake_still_happens_after_settle(pipeline) -> None:
    risky = {
        **TX,
        "transaction_id": "V6-RISK",
        "amount": 60_000,
        "currency": "EUR",
        "device_id": "DEV-NEW-01",
        "country": "NG",
        "location": "Lagos",
    }
    result = await pipeline.ingest(risky)
    assert result is not None and result["decision"] in ("HOLD", "BLOCK")
    await pipeline.settle()
    cases = await pipeline.cases.list_cases(customer_id="CUST-0003")
    assert cases
    case = await pipeline.cases.get_case(int(cases[0]["id"]))
    assert any(a["transaction_id"] == "V6-RISK" for a in case["alerts"])


async def test_redis_claim_makes_replays_idempotent_across_workers(app_env, monkeypatch) -> None:
    """Two pipelines sharing one Redis behave like two uvicorn workers."""
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    a = await build_pipeline(get_settings(), redis=redis)
    b = await build_pipeline(get_settings(), redis=redis)
    await a.start()
    await b.start()
    try:
        first = await a.ingest(dict(TX))
        assert first is not None
        replay = await b.ingest(dict(TX))
        assert replay is not None and replay["duplicate"] is True
        assert replay["decision"] == first["decision"]
        assert replay["risk_score"] == pytest.approx(first["risk_score"])
        assert "V6-1" not in b.results  # worker B did not score it again
    finally:
        await a.stop()
        await b.stop()


async def test_idempotency_claim_waits_for_pending_owner() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    owner, other = IdempotencyIndex(redis), IdempotencyIndex(redis)
    assert await owner.claim("X-1") is True
    assert await other.claim("X-1") is False
    waiter = asyncio.create_task(other.claimed_result("X-1", wait_s=2))
    await asyncio.sleep(0.01)
    await owner.record("X-1", {"transaction_id": "X-1", "decision": "ALLOW"})
    assert (await waiter) == {"transaction_id": "X-1", "decision": "ALLOW"}
    assert await other.claim("X-1") is False  # the recorded decision is kept
    # a claim that produced no decision is released for a retry
    assert await owner.claim("X-2") is True
    await owner.release("X-2")
    assert await other.claim("X-2") is True
    await owner.close()
    await other.close()


async def test_sse_ticket_is_shared_across_workers_and_single_use() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker_a, worker_b = TicketStore(redis=redis), TicketStore(redis=redis)
    ticket, ttl = await worker_a.issue(Principal("analist", "analist", "Analist"))
    assert ttl == get_settings().sse_ticket_ttl_s
    principal = await worker_b.redeem(ticket)
    assert principal is not None and principal.username == "analist"
    assert principal.role == "analist"
    assert await worker_a.redeem(ticket) is None  # single use on every worker
    assert await redis.ttl(f"sse:ticket:{ticket}") == -2


async def test_in_process_ticket_store_without_redis() -> None:
    store = TicketStore()
    ticket, _ = await store.issue(Principal("analist", "analist"))
    assert (await store.redeem(ticket)) is not None
    assert (await store.redeem(ticket)) is None


def test_scalable_bloom_grows_without_false_negatives() -> None:
    bloom = ScalableBloom(100)
    for i in range(1_000):
        bloom.add(f"TX-{i}")
    assert len(bloom.layers) > 1 and bloom.count == 1_000
    assert all(f"TX-{i}" in bloom for i in range(1_000))
