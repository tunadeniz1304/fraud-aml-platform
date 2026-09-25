"""M1: idempotency keys carry the payload digest and a short, refreshed lease.

* the same ``transaction_id`` with a different payload is a 409 conflict;
* a PENDING claim expires quickly when its owner dies and is refreshed while
  the owner is alive;
* a retry while another worker is still scoring gets a fast 409 "processing"
  answer (``Retry-After``) instead of a 30 s wait and a re-score;
* the Redis record is provisional until the writer committed the decision.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import create_app
from app.api.state import state
from app.bus.base import RetryLater
from app.config import get_settings
from app.idempotency import (
    PENDING,
    IdempotencyConflict,
    IdempotencyIndex,
    IngestInProgress,
    payload_digest,
)
from app.pipeline import build_pipeline

TX: dict[str, Any] = {
    "transaction_id": "M1-1",
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


def _redis() -> Any:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture()
async def workers(app_env, monkeypatch):
    """Two pipelines sharing one Redis and one DB (two uvicorn workers)."""
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    redis = _redis()
    a = await build_pipeline(get_settings(), redis=redis)
    b = await build_pipeline(get_settings(), redis=redis)
    await a.start()
    await b.start()
    yield a, b, redis
    await a.stop()
    await b.stop()


# --- digest -----------------------------------------------------------------
def test_payload_digest_ignores_sender_and_key_order() -> None:
    reordered = dict(reversed(list(TX.items())))
    assert payload_digest(TX) == payload_digest(reordered)
    assert payload_digest(TX) == payload_digest({**TX, "ingested_by": "svc-a"})
    assert payload_digest(TX) != payload_digest({**TX, "amount": 421})


# --- IdempotencyIndex ---------------------------------------------------------
async def test_claim_with_another_payload_is_a_conflict() -> None:
    redis = _redis()
    owner, other = IdempotencyIndex(redis), IdempotencyIndex(redis)
    assert await owner.claim("X-1", "aaa")
    with pytest.raises(IdempotencyConflict):
        await other.claimed_result("X-1", "bbb", wait_s=0)
    await owner.record("X-1", {"transaction_id": "X-1"}, "aaa")
    with pytest.raises(IdempotencyConflict):
        await other.claimed_result("X-1", "bbb")
    assert await other.claimed_result("X-1", "aaa") == {"transaction_id": "X-1"}
    await owner.close()


async def test_pending_retry_gets_processing_quickly_not_a_30s_wait() -> None:
    redis = _redis()
    owner = IdempotencyIndex(redis)
    other = IdempotencyIndex(redis, pending_wait_s=0.05)
    assert await owner.claim("X-1", "aaa")
    started = time.monotonic()
    with pytest.raises(IngestInProgress) as caught:
        await other.claimed_result("X-1", "aaa")
    assert time.monotonic() - started < 1.0
    assert isinstance(caught.value, RetryLater)  # the Redis bus retries it later
    assert caught.value.retry_after_s >= 1
    await owner.close()


async def test_pending_lease_is_short_refreshed_and_expires_after_a_crash() -> None:
    redis = _redis()
    owner = IdempotencyIndex(redis, pending_ttl_s=0.3)
    other = IdempotencyIndex(redis, pending_ttl_s=0.3)
    assert await owner.claim("X-1", "aaa")
    assert 0 < await redis.pttl("idem:X-1") <= 300
    await asyncio.sleep(0.7)  # > 2 leases: kept alive by the owner's refresher
    assert (await redis.get("idem:X-1")).startswith(f"{PENDING}:aaa:")
    await owner.close()  # the owner "crashes": nobody refreshes the lease
    await asyncio.sleep(0.45)
    assert await redis.get("idem:X-1") is None
    assert await other.claimed_result("X-1", "aaa") is None
    assert await other.claim("X-1", "aaa")
    await other.close()


async def test_release_does_not_delete_a_claim_taken_over_by_another_worker() -> None:
    redis = _redis()
    stale, fresh = IdempotencyIndex(redis), IdempotencyIndex(redis)
    assert await stale.claim("X-1", "aaa")
    await redis.delete("idem:X-1")  # lease expired ...
    assert await fresh.claim("X-1", "aaa")  # ... and another worker took over
    taken = await redis.get("idem:X-1")
    await stale.release("X-1")
    assert await redis.get("idem:X-1") == taken
    await stale.close()
    await fresh.close()


async def test_provisional_record_is_served_to_http_but_not_to_durable_ingress() -> None:
    redis = _redis()
    owner = IdempotencyIndex(redis, ttl_s=3600)
    other = IdempotencyIndex(redis, pending_wait_s=0.05)
    assert await owner.claim("X-1", "aaa")
    await owner.record("X-1", {"decision": "ALLOW"}, "aaa", committed=False)
    assert await other.claimed_result("X-1", "aaa") == {"decision": "ALLOW"}
    with pytest.raises(IngestInProgress):
        await other.claimed_result("X-1", "aaa", require_committed=True)
    assert await redis.pttl("idem:X-1") <= 45_000  # still on the short lease
    await owner.commit("X-1")
    assert await other.claimed_result("X-1", "aaa", require_committed=True) == {"decision": "ALLOW"}
    assert await redis.ttl("idem:X-1") > 3000
    await owner.close()


# --- pipeline ----------------------------------------------------------------------
async def test_same_id_with_another_payload_is_rejected_on_every_worker(workers) -> None:
    a, b, _ = workers
    first = await a.ingest(dict(TX))
    assert first is not None
    changed = {**TX, "amount": 99_999}
    with pytest.raises(IdempotencyConflict):
        await a.ingest(dict(changed))  # in-process result cache
    with pytest.raises(IdempotencyConflict):
        await b.ingest(dict(changed))  # other worker, Redis record
    replay = await b.ingest({**TX, "ingested_by": "another-client"})
    assert replay is not None and replay["duplicate"] is True
    assert replay["decision"] == first["decision"]


async def test_retry_during_pending_answers_processing_without_rescoring(workers) -> None:
    _, b, redis = workers
    # worker A holds the claim (still scoring)
    await redis.set("idem:M1-1", f"{PENDING}:{payload_digest(TX)}", px=45_000)
    started = time.monotonic()
    with pytest.raises(IngestInProgress):
        await b.ingest(dict(TX))
    assert time.monotonic() - started < 2.0
    assert "M1-1" not in b.results  # not scored a second time


async def test_crashed_owner_blocks_the_id_only_for_one_short_lease(workers) -> None:
    _, b, redis = workers
    await redis.set("idem:M1-1", f"{PENDING}:{payload_digest(TX)}", px=300)
    with pytest.raises(IngestInProgress):
        await b.ingest(dict(TX))
    await asyncio.sleep(0.4)
    result = await b.ingest(dict(TX))
    assert result is not None and not result.get("duplicate")


async def test_redis_record_is_committed_only_after_the_writer_commit(workers) -> None:
    a, _, redis = workers
    await a.ingest(dict(TX))
    await a.wait_background()  # _finalize: writer barrier -> commit
    record = json.loads(await redis.get("idem:M1-1"))
    assert record["_committed"] is True and record["_digest"] == payload_digest(TX)
    assert await redis.ttl("idem:M1-1") > 3600


async def test_durable_ingress_does_not_accept_an_uncommitted_decision(workers) -> None:
    _, b, redis = workers
    provisional = {"transaction_id": "M1-1", "decision": "ALLOW"}
    value = {**provisional, "_digest": payload_digest(TX), "_committed": False}
    await redis.set("idem:M1-1", json.dumps(value), px=45_000)
    with pytest.raises(IngestInProgress):
        await b.ingest(dict(TX), durable=True)
    served = await b.ingest(dict(TX))  # HTTP may answer with the decision
    assert served is not None and served["decision"] == "ALLOW"


async def test_conflict_is_detected_against_the_database_after_a_restart(
    app_env, monkeypatch
) -> None:
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    first = await build_pipeline(get_settings())
    await first.start()
    await first.ingest(dict(TX))
    await first.stop()
    restarted = await build_pipeline(get_settings())
    await restarted.start()
    try:
        with pytest.raises(IdempotencyConflict):
            await restarted.ingest({**TX, "amount": 1})
        stored = await restarted.ingest(dict(TX))
        assert stored is not None and stored["persisted_only"] is True
    finally:
        await restarted.stop()


def test_http_answers_409_for_conflict_and_processing(app_env, analyst_headers) -> None:
    with TestClient(create_app()) as client:
        ok = client.post("/api/transactions", json=TX, headers=analyst_headers)
        assert ok.status_code == 200, ok.text
        again = client.post("/api/transactions", json=TX, headers=analyst_headers)
        assert again.status_code == 200 and again.json()["duplicate"] is True
        clash = client.post("/api/transactions", json={**TX, "amount": 7}, headers=analyst_headers)
        assert clash.status_code == 409
        assert clash.json()["code"] == "IDEMPOTENCY_CONFLICT"

        async def busy(tx: dict[str, Any], **_: Any) -> dict[str, Any]:
            raise IngestInProgress(str(tx["transaction_id"]), retry_after_s=2)

        state.pipeline.ingest = busy  # type: ignore[method-assign]
        pending = client.post(
            "/api/transactions", json={**TX, "transaction_id": "M1-2"}, headers=analyst_headers
        )
        assert pending.status_code == 409
        assert pending.json()["code"] == "PROCESSING"
        assert pending.headers["Retry-After"] == "2"
