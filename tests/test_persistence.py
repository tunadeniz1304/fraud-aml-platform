"""Persistence tests (P0.3): schema/migrations, money, hash-chained audit,
write-behind writer, account service and pipeline-level idempotent replay."""

from __future__ import annotations

import asyncio
import itertools
import random
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from conftest import make_store
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from alembic import command
from app.config import BASE_DIR, get_settings
from app.db import repository as repo
from app.db.audit import GENESIS, AuditEntry, verify_chain
from app.db.base import Base
from app.db.database import Database
from app.db.models import AccountStatusHistory, AuditLog, Decision, Transaction
from app.db.writer import PersistenceWriter
from app.pipeline import build_pipeline


# --- schema / migrations ---------------------------------------------------------------
def test_alembic_migrations_match_models(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'mig.db').as_posix()}"
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")

    async def diff() -> list:
        db = Database(url)
        async with db.engine.connect() as conn:
            result = await conn.run_sync(
                lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
            )
        await db.dispose()
        return result

    assert asyncio.run(diff()) == []
    command.downgrade(cfg, "base")


async def test_money_is_exact_decimal(store):
    tx = {
        "transaction_id": "TX-M",
        "ts": "2026-09-22T10:00:00",
        "customer_id": "CUST-0001",
        "amount": 12345.675,
        "currency": "EUR",
        "amount_try": 555555.375,
    }
    async with store.db.transaction() as session:
        await repo.insert_transactions(session, [repo.transaction_values(tx)])
    async with store.db.session() as session:
        row = (await session.execute(select(Transaction))).scalar_one()
    assert row.amount == Decimal("12345.68") and row.amount_try == Decimal("555555.38")
    assert row.currency == "EUR" and row.ts.tzinfo is not None


async def test_upsert_customers_preserves_status(store):
    await store.set_status("CUST-0001", "BLOKE")
    directory = store.accounts  # re-seeding must not reset BLOKE
    async with store.db.transaction() as session:
        from app.features.extractor import CustomerDirectory

        records = CustomerDirectory.from_file(get_settings().resolved_customers_path).records()
        assert await repo.upsert_customers(session, records) == 5
    await directory.load()
    assert await store.db_status("CUST-0001") == "BLOKE"


# --- audit chain -----------------------------------------------------------------------------
async def _write_chain(store, n: int = 5) -> None:
    for i in range(n):
        await store.writer.record_audit(
            AuditEntry(
                event_type="DECISION",
                transaction_id=f"T{i}",
                customer_id="CUST-0001",
                risk_score=0.1 * i,
                decision="GECTI",
                reason=f"satır {i}",
                payload={"i": i, "nested": {"ok": True}},
            )
        )


async def test_audit_chain_verifies_and_links(store):
    async with store.db.session() as session:
        empty = await verify_chain(session)
    assert empty.ok and empty.checked == 0 and empty.head_hash == GENESIS
    await _write_chain(store)
    async with store.db.session() as session:
        result = await verify_chain(session)
        rows = list((await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars())
    assert result.ok and result.checked == 5
    assert rows[0].prev_hash == GENESIS
    assert all(b.prev_hash == a.hash for a, b in itertools.pairwise(rows))
    assert result.head_hash == rows[-1].hash


@pytest.mark.parametrize(
    "tamper",
    [
        {"reason": "değiştirildi"},
        {"risk_score": 0.99},
        {"decision": "BLOKE"},
        {"payload": {"i": 2, "nested": {"ok": False}}},
    ],
)
async def test_audit_tampering_is_detected(store, tamper):
    await _write_chain(store)
    async with store.db.transaction() as session:
        target = (
            await session.execute(select(AuditLog).where(AuditLog.reason == "satır 2"))
        ).scalar_one()
        await session.execute(update(AuditLog).where(AuditLog.id == target.id).values(**tamper))
    async with store.db.session() as session:
        result = await verify_chain(session)
    assert not result.ok and result.first_broken_id == target.id
    assert result.checked == 2


async def test_audit_deletion_is_detected(store):
    await _write_chain(store)
    async with store.db.transaction() as session:
        row = (
            await session.execute(select(AuditLog).where(AuditLog.reason == "satır 1"))
        ).scalar_one()
        await session.delete(row)
    async with store.db.session() as session:
        result = await verify_chain(session)
    assert not result.ok and "zincir" in result.detail


# --- writer ------------------------------------------------------------------------------------
async def test_writer_batches_and_flushes(store):
    writer: PersistenceWriter = store.writer
    await writer.start()
    for i in range(1_200):
        await writer.record_audit(AuditEntry(event_type="DECISION", entity_id=f"E{i}"))
    await writer.flush()
    assert await store.count(AuditLog) == 1_200
    assert writer.batches_written < 1_200  # actually batched
    await writer.stop()


async def test_writer_backpressure_bounds_queue(tmp_path):
    platform = await make_store(tmp_path / "bp.db")
    writer = PersistenceWriter(platform.db, queue_size=10, batch_size=5, flush_interval=0.001)
    original = writer._apply

    async def slow(batch):
        await asyncio.sleep(0.005)
        await original(batch)

    writer._apply = slow  # type: ignore[method-assign]
    await writer.start()
    peak = 0
    for i in range(100):
        await writer.record_audit(AuditEntry(event_type="DECISION", entity_id=str(i)))
        peak = max(peak, writer.backlog)
    await writer.stop()
    assert peak <= 10
    async with platform.db.session() as session:
        assert await repo.count(session, AuditLog) == 100
    await platform.db.dispose()


async def test_writer_survives_a_failing_batch(store):
    writer = store.writer
    await writer.start()
    original = writer._apply
    calls = {"n": 0}

    async def flaky(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("DB geçici hata")
        await original(batch)

    writer._apply = flaky  # type: ignore[method-assign]
    await writer.record_audit(AuditEntry(event_type="DECISION", entity_id="lost"))
    await writer.flush()
    await writer.record_audit(AuditEntry(event_type="DECISION", entity_id="kept"))
    await writer.flush()
    assert writer.errors == 1
    assert [r["transaction_id"] for r in await store.audit()] == ["kept"]


# --- account service ------------------------------------------------------------------------------
async def test_escalation_is_monotonic_and_audited(store):
    accounts = store.accounts
    assert await accounts.escalate("CUST-0003", "INCELENIYOR", reason="r1") == (
        "AKTIF",
        "INCELENIYOR",
    )
    assert await accounts.escalate("CUST-0003", "AKTIF", reason="r2") == (
        "INCELENIYOR",
        "INCELENIYOR",
    )
    assert await accounts.escalate("CUST-0003", "BLOKE", reason="r3") == ("INCELENIYOR", "BLOKE")
    assert await store.db_status("CUST-0003") == "BLOKE"
    async with store.db.session() as session:
        history = list((await session.execute(select(AccountStatusHistory))).scalars())
    assert [(h.from_status, h.to_status) for h in history] == [
        ("AKTIF", "INCELENIYOR"),
        ("INCELENIYOR", "BLOKE"),
    ]
    await store.set_status("CUST-0003", "AKTIF")  # analyst may de-escalate
    assert accounts.get_status("CUST-0003") == "AKTIF"
    assert accounts.counts()["AKTIF"] == 5


# --- pipeline replay ------------------------------------------------------------------------------
def _synthetic(n: int, seed: int = 11) -> list[dict]:
    rng = random.Random(seed)
    start = datetime(2026, 9, 1, 8, 0)
    customers = ["CUST-0001", "CUST-0002", "CUST-0003", "CUST-0004", "CUST-0005"]
    devices = {
        "CUST-0001": "DEV-9F2A-11",
        "CUST-0002": "DEV-11AA-22",
        "CUST-0003": "DEV-88CC-11",
        "CUST-0004": "DEV-BB44-77",
        "CUST-0005": "DEV-CC10-FF",
    }
    out = []
    for i in range(n):
        cust = rng.choice(customers)
        out.append(
            {
                "transaction_id": f"R-{i:05d}",
                "ts": (start + timedelta(seconds=37 * i)).isoformat(),
                "customer_id": cust,
                "amount": round(rng.uniform(50, 900), 2),
                "currency": "TRY",
                "device_id": devices[cust],
                "location": "İstanbul",
                "country": "TR",
                "beneficiary_id": f"B-{rng.randint(1, 40)}",
                "channel": "mobile",
            }
        )
    return out


@pytest.mark.slow
async def test_10k_replay_through_pipeline_is_idempotent(app_env, monkeypatch):
    monkeypatch.setenv("STREAM_MODE", "off")
    monkeypatch.setenv("BUS_PARTITIONS", "4")
    get_settings.cache_clear()
    pipeline = await build_pipeline(get_settings())
    await pipeline.start()
    stream = _synthetic(10_000)
    await pipeline.feed(stream)
    await pipeline.feed(stream)  # full replay
    await pipeline.drain()
    async with pipeline.db.session() as session:
        assert await repo.count(session, Transaction) == 10_000
        assert await repo.count(session, Decision) == 10_000
        decided = (await session.execute(select(Decision.transaction_id))).scalars().all()
        assert len(set(decided)) == 10_000
        assert (await verify_chain(session)).ok
    assert pipeline.bus.duplicates >= 10_000
    assert not pipeline.bus.dlq
    await pipeline.stop()


def test_dlq_entries_are_visible_over_api(app_env, analyst_headers):
    from app.api.dashboard import create_app
    from app.api.state import state

    with TestClient(create_app()) as client:

        def broken(payload):
            raise RuntimeError("tüketici arızası")

        state.pipeline.bus.subscribe("decision.made", broken)
        r = client.post(
            "/api/transactions",
            json={
                "transaction_id": "TX-DLQ",
                "ts": "2026-09-22T15:00:00",
                "customer_id": "CUST-0003",
                "amount": 100,
                "currency": "TRY",
                "device_id": "DEV-88CC-11",
                "location": "İzmir",
            },
            headers=analyst_headers,
        )
        assert r.status_code == 200
        dlq = client.get("/api/bus/dlq", headers=analyst_headers).json()
        assert dlq["size"] >= 1
        assert any(e["key"] == "TX-DLQ" and "arızası" in e["error"] for e in dlq["entries"])
        verify = client.get("/api/audit/verify", headers=analyst_headers).json()
        assert verify["ok"] is True and verify["checked"] >= 10
