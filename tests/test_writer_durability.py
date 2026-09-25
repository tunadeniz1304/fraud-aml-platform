"""H4: the write-behind writer never drops a batch silently.

Retry with backoff -> bisection -> poison operations to ``dead_letters``;
every good operation of the batch is committed. ``barrier()`` resolves only
after the operations submitted before it are committed.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db import repository as repo
from app.db.audit import AuditEntry, verify_chain
from app.db.models import AuditLog, DeadLetterRecord, Decision, Transaction
from app.db.writer import PersistenceWriter, is_transient


def _tx(i: int) -> dict:
    return repo.transaction_values(
        {
            "transaction_id": f"TX-D{i:04d}",
            "ts": "2026-09-22T10:00:00",
            "customer_id": "CUST-0001",
            "amount": 100 + i,
        }
    )


def _decision(tx_id: str) -> dict:
    return {
        "transaction_id": tx_id,
        "customer_id": "CUST-0001",
        "decision": "ALLOW",
        "risk_score": 0.1,
    }


def _audit(tx_id: str) -> AuditEntry:
    return AuditEntry(event_type="DECISION", transaction_id=tx_id, entity_id=tx_id)


async def _dead_letters(store) -> list[DeadLetterRecord]:
    async with store.db.session() as session:
        return list((await session.execute(select(DeadLetterRecord))).scalars())


async def test_poison_op_is_dead_lettered_and_every_good_op_is_committed(store):
    writer = PersistenceWriter(store.db, retry_base=0.001, retry_max=0.002)
    await writer.start()
    for i in range(60):
        tx = _tx(i)
        if i == 37:  # NOT NULL violation: permanent, not transient
            tx = {**tx, "customer_id": None}
        await writer.record_decision(tx, _decision(tx["id"]), _audit(tx["id"]))
    await writer.flush()
    await writer.stop()

    async with store.db.session() as session:
        assert await repo.count(session, Transaction) == 59
        assert await repo.count(session, Decision) == 60  # its decision row is fine
        assert await repo.count(session, AuditLog) == 60
        assert (await verify_chain(session)).ok
    dead = await _dead_letters(store)
    assert [(d.kind, d.key) for d in dead] == [("transaction", "TX-D0037")]
    assert dead[0].payload["id"] == "TX-D0037" and "NOT NULL" in dead[0].error.upper()
    assert writer.dead_lettered == 1 and writer.lost == 0


async def test_transient_errors_are_retried_until_the_batch_commits(store):
    writer = PersistenceWriter(store.db, retry_base=0.001, retry_max=0.002)
    original = writer._apply
    calls = {"n": 0}

    async def locked(batch):
        calls["n"] += 1
        if calls["n"] <= 5:  # more than max_retries: transient errors never bisect
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        await original(batch)

    writer._apply = locked  # type: ignore[method-assign]
    await writer.start()
    for i in range(10):
        tx = _tx(i)
        await writer.record_decision(tx, _decision(tx["id"]), _audit(tx["id"]))
    await writer.flush()
    await writer.stop()
    async with store.db.session() as session:
        assert await repo.count(session, Transaction) == 10
    assert writer.retries == 5 and await _dead_letters(store) == []


async def test_dead_letter_failure_is_logged_with_payload_not_dropped(store, caplog):
    writer = PersistenceWriter(store.db, max_retries=0, retry_base=0.001, retry_max=0.001)

    async def broken(batch):
        raise ValueError("şema bozuk")

    writer._apply = broken  # type: ignore[method-assign]
    original_tx = store.db.transaction

    def no_db():
        raise RuntimeError("DB kapalı")

    store.db.transaction = no_db  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.CRITICAL, logger="fraud.db.writer"):
            await writer.record_audit(_audit("TX-LOST"))
    finally:
        store.db.transaction = original_tx  # type: ignore[method-assign]
    assert writer.lost == 1
    assert any("TX-LOST" in r.getMessage() for r in caplog.records)


async def test_barrier_waits_for_the_commit(store):
    writer = PersistenceWriter(store.db, flush_interval=0.05)
    await writer.start()
    tx = _tx(1)
    await writer.record_decision(tx, _decision(tx["id"]), _audit(tx["id"]))
    async with store.db.session() as session:
        assert await repo.count(session, Transaction) == 0  # still write-behind
    await writer.barrier()
    async with store.db.session() as session:
        assert await repo.count(session, Transaction) == 1
    await writer.stop()


async def test_barrier_does_not_hang_when_the_writer_is_stopped(store):
    writer = PersistenceWriter(store.db)
    await asyncio.wait_for(writer.barrier(), 1)


def test_transient_classification():
    assert is_transient(OperationalError("x", {}, Exception("database is locked")))
    assert is_transient(ConnectionResetError())
    assert not is_transient(OperationalError("x", {}, Exception("no such table: foo")))
    assert not is_transient(ValueError("boom"))
