"""M13 — durable case intake through the ``case_outbox`` table.

The outbox row commits in the decision's own writer batch; the write-behind
intake deletes it, and a crash or failed intake leaves it for the replayer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select

from app.cases.outbox import CaseOutboxReplayer, needs_outbox, outbox_row
from app.config import get_settings
from app.db.models import Alert, CaseOutbox, DeadLetterRecord
from app.pipeline import Pipeline, build_pipeline

CID = "CUST-0003"
TX: dict[str, Any] = {
    "transaction_id": "OB-1",
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


@pytest.fixture()
async def pipeline(app_env, monkeypatch) -> AsyncIterator[Pipeline]:
    monkeypatch.setenv("STREAM_MODE", "off")
    get_settings.cache_clear()
    p = await build_pipeline(get_settings())
    await p.start()
    try:
        yield p
    finally:
        await p.stop()
        get_settings.cache_clear()


async def _count(p: Pipeline, model: Any, *where: Any) -> int:
    async with p.db.session() as session:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int(await session.scalar(stmt) or 0)


async def _hold(p: Pipeline, tx_id: str) -> dict[str, Any]:
    await p.set_thresholds(0.0001, 0.0002, 0.9999)  # every scored event is a HOLD
    result = await p.ingest(dict(TX, transaction_id=tx_id))
    assert result is not None and result["decision"] == "HOLD"
    await p.drain()
    return result


def test_outbox_predicate_is_a_superset_of_the_alert_rule():
    assert needs_outbox({"decision": "HOLD"}) and needs_outbox({"decision": "BLOCK"})
    assert needs_outbox({"decision": "PASS", "case_required": True})
    assert not needs_outbox({"decision": "PASS"})
    row = outbox_row({"transaction_id": "X", "decision": "HOLD", "odd": object()})
    assert row["transaction_id"] == "X" and isinstance(row["payload"]["odd"], str)


async def test_live_intake_creates_the_alert_and_clears_the_outbox(pipeline):
    await _hold(pipeline, "OB-LIVE")
    assert await _count(pipeline, Alert, Alert.transaction_id == "OB-LIVE") == 1
    assert await _count(pipeline, CaseOutbox) == 0


async def test_pass_decision_writes_no_outbox_row(pipeline):
    await pipeline.set_thresholds(0.97, 0.98, 0.99)
    result = await pipeline.ingest(dict(TX, transaction_id="OB-PASS"))
    assert result is not None and result["decision"] not in ("HOLD", "BLOCK")
    await pipeline.drain()
    if not result.get("case_required"):
        assert await _count(pipeline, CaseOutbox) == 0


async def test_failed_intake_is_replayed_from_the_outbox(pipeline, monkeypatch):
    real = pipeline.cases.on_decision

    async def broken(event: dict[str, Any]) -> int | None:
        raise RuntimeError("case DB unavailable")

    monkeypatch.setattr(pipeline.cases, "on_decision", broken)
    await _hold(pipeline, "OB-LOST")
    assert await _count(pipeline, Alert, Alert.transaction_id == "OB-LOST") == 0
    assert await _count(pipeline, CaseOutbox) == 1  # committed with the decision

    # rows younger than case_outbox_replay_s are left to the live path
    assert await pipeline.outbox.replay() == 0

    pipeline.outbox.intake = real
    assert await pipeline.outbox.replay(min_age_s=0) == 1
    assert await _count(pipeline, Alert, Alert.transaction_id == "OB-LOST") == 1
    assert await _count(pipeline, CaseOutbox) == 0
    assert pipeline.outbox.replayed == 1


async def test_lost_done_marker_does_not_duplicate_the_alert(pipeline):
    await _hold(pipeline, "OB-DUP")
    async with pipeline.db.transaction() as session:  # intake ran, delete was lost
        session.add(CaseOutbox(**outbox_row(dict(TX, transaction_id="OB-DUP", decision="HOLD"))))
    assert await pipeline.outbox.replay(min_age_s=0) == 1
    assert await _count(pipeline, Alert, Alert.transaction_id == "OB-DUP") == 1
    assert await _count(pipeline, CaseOutbox) == 0


async def test_concurrent_replayers_create_one_alert(pipeline):
    async with pipeline.db.transaction() as session:
        session.add(CaseOutbox(**outbox_row(dict(TX, transaction_id="OB-RACE", decision="HOLD"))))
    other = CaseOutboxReplayer(pipeline.db, pipeline.cases.on_decision, replay_s=120)
    settled = await asyncio.gather(pipeline.outbox.replay(min_age_s=0), other.replay(min_age_s=0))
    assert sum(settled) == 1
    assert await _count(pipeline, Alert, Alert.transaction_id == "OB-RACE") == 1
    assert await _count(pipeline, CaseOutbox) == 0


async def test_row_is_dead_lettered_after_repeated_failures(pipeline):
    async def broken(event: dict[str, Any]) -> int | None:
        raise RuntimeError("still down")

    async with pipeline.db.transaction() as session:
        session.add(CaseOutbox(**outbox_row(dict(TX, transaction_id="OB-DLQ", decision="HOLD"))))
    replayer = CaseOutboxReplayer(pipeline.db, broken, replay_s=120, max_attempts=3)
    for _ in range(3):
        assert await replayer.replay(min_age_s=0) == 0
    assert await replayer.replay(min_age_s=0) == 1
    assert await _count(pipeline, CaseOutbox) == 0
    assert (
        await _count(
            pipeline,
            DeadLetterRecord,
            DeadLetterRecord.source == "case_outbox",
            DeadLetterRecord.key == "OB-DLQ",
        )
        == 1
    )
    assert replayer.dead == 1
