"""Durable case intake (M13).

A decision that needs an alert writes a ``case_outbox`` row in the same writer
batch (same DB transaction) as its transaction/decision/audit rows, so the
hot path never waits for it.  The in-memory write-behind queue then creates the
alert/case as before and deletes the row (``outbox_done``) once that worked.

If the process dies between the decision commit and the case intake, or the
intake fails, the row stays behind.  :class:`CaseOutboxReplayer` picks rows up
that are older than ``case_outbox_replay_s`` (so rows still in flight on the
live path are left alone), claims each one with a compare-and-set on
``attempts`` that also renews ``created_at`` (a lease: other workers skip it
for another ``replay_s``), skips transactions that already have an alert and
otherwise re-runs ``CaseService.on_decision``.  Rows that keep failing are moved
to ``dead_letters``.

Remaining gap: only case intake is durable.  The live dashboard fan-out and
the egress (downstream) publication stay memory-only best-effort effects; a
crash can drop them for the decisions that were in the write-behind queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update

from app.db.base import utcnow
from app.db.models import Alert, CaseOutbox, DeadLetterRecord

logger = logging.getLogger("fraud.cases.outbox")

MAX_ATTEMPTS = 5


def needs_outbox(event: dict[str, Any]) -> bool:
    """Superset of ``CaseService.needs_alert``: a row too many is dropped by
    the replay (``on_decision`` returns ``None``), a row too few is lost."""
    return bool(event.get("case_required")) or event.get("decision") in ("HOLD", "BLOCK")


def outbox_row(event: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe outbox record for a DECIDED event."""
    payload = json.loads(json.dumps(event, default=str))
    return {
        "transaction_id": str(event["transaction_id"]),
        "payload": payload,
        "attempts": 0,
        "created_at": utcnow(),
    }


class CaseOutboxReplayer:
    def __init__(
        self,
        db: Any,
        intake: Callable[[dict[str, Any]], Awaitable[int | None]],
        *,
        replay_s: float,
        on_case: Callable[[int, dict[str, Any]], None] | None = None,
        batch: int = 100,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.db = db
        self.intake = intake
        self.replay_s = replay_s
        self.on_case = on_case
        self.batch = batch
        self.max_attempts = max_attempts
        self._task: asyncio.Task[None] | None = None
        self.replayed = 0
        self.dead = 0

    # --- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        if self.replay_s > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="case-outbox-replay")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.replay()
            except asyncio.CancelledError:
                raise
            except Exception:  # the loop must survive a DB hiccup
                logger.exception("[Outbox] vaka kuyruğu yeniden oynatılamadı")
            await asyncio.sleep(self.replay_s)

    # --- replay ---------------------------------------------------------------------
    async def replay(self, *, min_age_s: float | None = None) -> int:
        """Replay stale outbox rows; returns how many were settled."""
        age = self.replay_s if min_age_s is None else min_age_s
        cutoff = utcnow() - timedelta(seconds=age)
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(CaseOutbox.transaction_id, CaseOutbox.attempts)
                    .where(CaseOutbox.created_at <= cutoff)
                    .order_by(CaseOutbox.created_at)
                    .limit(self.batch)
                )
            ).all()
        settled = 0
        for tx_id, attempts in rows:
            if await self._replay_one(str(tx_id), int(attempts or 0)):
                settled += 1
        return settled

    async def _claim(self, tx_id: str, attempts: int) -> dict[str, Any] | None:
        async with self.db.transaction() as session:
            result = await session.execute(
                update(CaseOutbox)
                .where(CaseOutbox.transaction_id == tx_id, CaseOutbox.attempts == attempts)
                .values(attempts=attempts + 1, created_at=utcnow())
            )
            if getattr(result, "rowcount", 0) != 1:
                return None  # another worker claimed (or settled) it
            payload = (
                await session.execute(
                    select(CaseOutbox.payload).where(CaseOutbox.transaction_id == tx_id)
                )
            ).scalar_one()
        return dict(payload or {})

    async def _delete(self, tx_id: str) -> None:
        async with self.db.transaction() as session:
            await session.execute(delete(CaseOutbox).where(CaseOutbox.transaction_id == tx_id))

    async def _alert_exists(self, tx_id: str) -> bool:
        async with self.db.session() as session:
            found = (
                await session.execute(
                    select(Alert.id).where(Alert.transaction_id == tx_id).limit(1)
                )
            ).first()
        return found is not None

    async def _replay_one(self, tx_id: str, attempts: int) -> bool:
        if attempts >= self.max_attempts:
            await self._dead_letter(tx_id, attempts)
            return True
        payload = await self._claim(tx_id, attempts)
        if payload is None:
            return False
        if await self._alert_exists(tx_id):
            await self._delete(tx_id)  # intake ran, only the done-marker was lost
            return True
        try:
            case_id = await self.intake(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # retried on the next pass, then dead-lettered
            logger.exception("[Outbox] %s vaka kaydı yeniden denenemedi", tx_id)
            return False
        await self._delete(tx_id)
        self.replayed += 1
        logger.warning("[Outbox] %s vaka kaydı yeniden oynatıldı (vaka %s)", tx_id, case_id)
        if case_id is not None and self.on_case is not None:
            self.on_case(case_id, payload)
        return True

    async def _dead_letter(self, tx_id: str, attempts: int) -> None:
        async with self.db.transaction() as session:
            row = (
                await session.execute(select(CaseOutbox).where(CaseOutbox.transaction_id == tx_id))
            ).scalar_one_or_none()
            if row is None:
                return
            session.add(
                DeadLetterRecord(
                    source="case_outbox",
                    kind="outbox",
                    key=tx_id,
                    error="case intake failed after retries",
                    attempts=attempts,
                    payload=dict(row.payload or {}),
                )
            )
            await session.delete(row)
        self.dead += 1
        logger.critical("[Outbox] %s vaka kaydı %d denemeden sonra DLQ'ya taşındı", tx_id, attempts)
