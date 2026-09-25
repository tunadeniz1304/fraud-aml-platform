"""M10 / H3 — account status compare-and-set and cross-worker cache sync.

Two :class:`AccountService` instances over one SQLite file stand in for two
workers: each has its own in-memory cache and its own write-behind writer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import PlatformStore, make_store
from sqlalchemy import select

from app.db import repository as repo
from app.db.models import AccountStatusHistory

CID = "CUST-0003"


@pytest.fixture()
async def pair(tmp_path: Path) -> AsyncIterator[tuple[PlatformStore, PlatformStore]]:
    path = tmp_path / "shared.db"
    a = await make_store(path)
    b = await make_store(path)
    yield a, b
    for store in (a, b):
        await store.writer.stop()
        await store.db.dispose()


async def _history(store: PlatformStore) -> list[tuple[str, str]]:
    async with store.db.session() as session:
        rows = (
            await session.execute(
                select(AccountStatusHistory)
                .where(AccountStatusHistory.customer_id == CID)
                .order_by(AccountStatusHistory.id)
            )
        ).scalars()
        return [(h.from_status, h.to_status) for h in rows]


async def test_stale_worker_cannot_downgrade_a_block(pair):
    a, b = pair
    await a.accounts.escalate(CID, "BLOKE", reason="worker A")
    await a.writer.flush()
    # B still caches AKTIF v1; its system escalation must not overwrite BLOKE
    assert b.get_status(CID) == "AKTIF"
    await b.accounts.escalate(CID, "INCELENIYOR", reason="worker B, stale")
    await b.writer.flush()
    assert await a.db_status(CID) == "BLOKE"
    assert await _history(a) == [("AKTIF", "BLOKE")]
    # the committed CAS result refreshed B's cache (after the commit)
    assert b.get_status(CID) == "BLOKE"
    assert b.accounts.get_version(CID) == 2


async def test_analyst_change_with_a_stale_version_rereads_the_row(pair):
    a, b = pair
    await a.accounts.escalate(CID, "BLOKE", reason="worker A")
    await a.writer.flush()
    previous, new = await b.accounts.set_by_analyst(CID, "INCELENIYOR", actor="u", reason="r")
    assert (previous, new) == ("BLOKE", "INCELENIYOR")  # true previous, not the stale cache
    assert await _history(a) == [("AKTIF", "BLOKE"), ("BLOKE", "INCELENIYOR")]
    assert b.accounts.get_version(CID) == 3
    audits = await b.audit("ACCOUNT_STATUS")
    assert "BLOKE->INCELENIYOR" in {r["decision"] for r in audits}


async def test_committed_cache_is_updated_only_after_the_commit(store):
    writer = store.writer
    await writer.start()
    gate = asyncio.Event()
    original = writer._apply

    async def slow(batch):
        await gate.wait()
        await original(batch)

    writer._apply = slow  # type: ignore[method-assign]
    await store.accounts.escalate(CID, "BLOKE", reason="r")
    await asyncio.sleep(0.1)
    # the hot path already enforces the block (pending overlay) ...
    assert store.get_status(CID) == "BLOKE"
    # ... but the committed cache still holds the last committed row
    assert store.accounts._status[CID] == "AKTIF"
    assert store.accounts.get_version(CID) == 1
    gate.set()
    await writer.flush()
    assert store.accounts._status[CID] == "BLOKE"
    assert store.accounts.get_version(CID) == 2
    assert CID not in store.accounts._pending


async def test_cas_gives_up_after_repeated_conflicts(store):
    async with store.db.transaction() as session:
        with pytest.raises(repo.AccountStatusConflict):
            await repo.transition_account_status(
                session,
                CID,
                "BLOKE",
                kind="system",
                reason="r",
                actor="system",
                expected=("AKTIF", 99),  # never matches ...
                attempts=1,  # ... and no re-read allowed
            )
    async with store.db.transaction() as session:
        result = await repo.transition_account_status(
            session, CID, "BLOKE", kind="system", reason="r", actor="system", expected=("AKTIF", 99)
        )
    assert result is not None and (result.previous, result.new, result.version) == (
        "AKTIF",
        "BLOKE",
        2,
    )


async def test_concurrent_escalations_from_two_workers_serialise(pair):
    a, b = pair
    await asyncio.gather(
        a.accounts.escalate(CID, "INCELENIYOR", reason="A"),
        b.accounts.escalate(CID, "BLOKE", reason="B"),
    )
    await asyncio.gather(a.writer.flush(), b.writer.flush())
    assert await a.db_status(CID) == "BLOKE"
    history = await _history(a)
    assert history[-1][1] == "BLOKE"
    assert len(history) <= 2
    async with a.db.session() as session:
        state = await repo.account_state(session, CID)
    assert state is not None and state[1] == len(history) + 1


# --- H3: other workers pick the change up -------------------------------------------------
async def test_block_on_one_worker_is_enforced_by_the_other_after_sync(pair):
    a, b = pair
    await a.accounts.escalate(CID, "BLOKE", reason="A blocks")
    await a.writer.flush()
    assert b.get_status(CID) == "AKTIF"
    assert await b.accounts.sync() == 1
    assert b.get_status(CID) == "BLOKE"
    # an analyst unblock on B reaches A the same way
    await b.accounts.set_by_analyst(CID, "AKTIF", actor="u", reason="cleared")
    await a.accounts.sync()
    assert a.get_status(CID) == "AKTIF"
    assert await a.accounts.sync() == 0  # re-reading the overlap is idempotent


async def test_sync_poller_propagates_within_one_second(pair):
    a, b = pair
    b.accounts.start_sync(0.05)
    try:
        await a.accounts.escalate(CID, "BLOKE", reason="A blocks")
        await a.writer.flush()
        started = time.monotonic()
        while b.get_status(CID) != "BLOKE":
            assert time.monotonic() - started < 1.0, "block not propagated within 1 s"
            await asyncio.sleep(0.01)
    finally:
        await b.accounts.stop_sync()


async def test_older_poll_does_not_drop_a_stricter_pending_escalation(pair):
    a, b = pair
    await a.accounts.escalate(CID, "INCELENIYOR", reason="A")
    await a.writer.flush()
    b.accounts._pending[CID] = ("BLOKE", time.monotonic())  # B's own write still queued
    await b.accounts.sync()
    assert b.get_status(CID) == "BLOKE"
    assert b.accounts._status[CID] == "INCELENIYOR"
