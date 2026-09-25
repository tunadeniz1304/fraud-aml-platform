"""Account status service.

The hot path needs the account status *before* scoring (bug #5) without a DB
round trip, so statuses are cached in memory (loaded at start-up) and every
change is persisted through the write-behind writer together with a
``account_status_history`` row and a hash-chained audit entry.

* :meth:`escalate` — system transitions, monotonic (state machine, bug #4);
* :meth:`set_by_analyst` — human decision (de-escalation allowed); written
  synchronously because it is rare and must be durable before returning.

Consistency (M10 / H3) — the ``accounts`` table is the source of truth:

* every write is a compare-and-set on ``accounts.version``
  (:func:`repo.transition_account_status`); a stale cache re-reads the row and
  re-applies the state machine, so a worker can never downgrade a ``BLOKE``
  written by another worker;
* the *committed* cache (status, version) is only updated **after** the commit
  (writer listener / after the analyst transaction). A local system escalation
  is visible to this worker's hot path at once through a *pending* overlay —
  it can only make decisions stricter, and is dropped once the commit (or a
  poll showing an equal/stricter status) lands, or after ``PENDING_MAX_AGE_S``
  if the write was dead-lettered;
* other workers' changes are picked up by :meth:`sync`, polled every
  ``ACCOUNT_STATUS_POLL_MS`` (default 500 ms) on the indexed
  ``accounts.updated_at``. Documented propagation window: writer flush
  (``WRITER_FLUSH_MS``, 50 ms) + one poll interval, i.e. under 1 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.account_state import Actor, next_status, normalise, rank
from app.db import repository as repo
from app.db.audit import AuditEntry
from app.db.database import Database
from app.db.writer import PersistenceWriter, StatusChange

logger = logging.getLogger("fraud.accounts")

#: re-read window behind the newest ``updated_at`` seen: a row stamped just
#: before a slow commit becomes visible late; versions make re-reads idempotent
SYNC_OVERLAP = timedelta(seconds=30)
#: a pending (uncommitted) local escalation older than this is dropped
PENDING_MAX_AGE_S = 60.0


class AccountNotFoundError(KeyError):
    """No account row for the customer."""


class AccountService:
    def __init__(self, db: Database, writer: PersistenceWriter) -> None:
        self.db = db
        self.writer = writer
        self._status: dict[str, str] = {}  # committed status
        self._version: dict[str, int] = {}  # committed accounts.version
        self._pending: dict[str, tuple[str, float]] = {}  # local, not yet committed
        self._watermark: datetime | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self.synced = 0  # remote changes applied by the poller
        writer.status_listeners.append(self._on_commit)

    async def load(self) -> int:
        async with self.db.session() as session:
            states = await repo.account_states(session)
        self._status = {cid: status for cid, (status, _, _) in states.items()}
        self._version = {cid: version for cid, (_, version, _) in states.items()}
        stamps = [ts for _, _, ts in states.values() if ts is not None]
        self._watermark = max(stamps) if stamps else None
        return len(self._status)

    # --- reads (hot path) -----------------------------------------------------------
    def get_status(self, customer_id: str) -> str | None:
        pending = self._pending.get(customer_id)
        if pending is not None:
            return pending[0]
        return self._status.get(customer_id)

    def get_version(self, customer_id: str) -> int | None:
        return self._version.get(customer_id)

    def exists(self, customer_id: str) -> bool:
        return customer_id in self._status

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for cid in self._status:
            status = self.get_status(cid) or ""
            out[status] = out.get(status, 0) + 1
        return out

    async def list_accounts(self) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            return await repo.list_accounts(session)

    async def get_account(self, customer_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            return await repo.get_account(session, customer_id)

    # --- committed cache (M10) --------------------------------------------------------
    def _remember(self, customer_id: str, status: str, version: int) -> bool:
        """Store a committed (status, version) unless the cache is already newer."""
        if version < self._version.get(customer_id, 0):
            return False
        self._status[customer_id] = status
        self._version[customer_id] = version
        pending = self._pending.get(customer_id)
        if pending is not None and rank(status) >= rank(pending[0]):
            self._pending.pop(customer_id, None)
        return True

    def _on_commit(self, result: repo.StatusResult) -> None:
        self._remember(result.customer_id, result.new, result.version)

    # --- cross-worker sync (H3) -------------------------------------------------------
    async def sync(self) -> int:
        """Apply status changes committed by any worker since the last poll."""
        since = (
            self._watermark - SYNC_OVERLAP
            if self._watermark is not None
            else datetime(1970, 1, 1, tzinfo=UTC)
        )
        async with self.db.session() as session:
            rows = await repo.accounts_changed_since(session, since)
        applied = 0
        for cid, status, version, ts in rows:
            if self._watermark is None or ts > self._watermark:
                self._watermark = ts
            if version > self._version.get(cid, 0) and self._remember(cid, status, version):
                applied += 1
        now = time.monotonic()
        for cid, (_, since_s) in list(self._pending.items()):
            if now - since_s > PENDING_MAX_AGE_S:  # write dead-lettered: trust the DB
                self._pending.pop(cid, None)
        self.synced += applied
        return applied

    def start_sync(self, interval_s: float) -> None:
        if interval_s > 0 and (self._sync_task is None or self._sync_task.done()):
            self._sync_task = asyncio.create_task(self._sync_loop(interval_s), name="account-sync")

    async def stop_sync(self) -> None:
        task, self._sync_task = self._sync_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _sync_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.sync()
            except Exception:  # a DB hiccup must not kill the poller
                logger.exception("[Hesap] durum senkronizasyonu başarısız")

    # --- writes -----------------------------------------------------------------------
    async def escalate(
        self,
        customer_id: str,
        requested: str,
        *,
        reason: str,
        transaction_id: str | None = None,
        actor: Actor = "system",
        risk_score: float | None = None,
    ) -> tuple[str, str]:
        """Apply the state machine; persists asynchronously. Returns (prev, new).

        The DB write is a compare-and-set against the committed version; this
        worker's hot path sees the new status at once (pending overlay)."""
        current = self.get_status(customer_id)
        if current is None:
            raise AccountNotFoundError(customer_id)
        target = next_status(current, requested, actor=actor)
        if target == current:
            return current, current
        self._pending[customer_id] = (target, time.monotonic())
        await self.writer.record_status(
            StatusChange(
                customer_id,
                self._status[customer_id],
                target,
                reason,
                actor,
                transaction_id,
                version=self._version.get(customer_id),
                kind=actor,
            ),
            AuditEntry(
                event_type="ACCOUNT_STATUS",
                actor=actor,
                entity_type="account",
                entity_id=customer_id,
                transaction_id=transaction_id,
                customer_id=customer_id,
                risk_score=risk_score,
                decision=f"{current}->{target}",
                reason=reason,
            ),
        )
        logger.warning("[Hesap] %s durumu %s → %s (%s)", customer_id, current, target, reason)
        return current, target

    async def set_by_analyst(
        self, customer_id: str, target: str, *, actor: str, reason: str
    ) -> tuple[str, str]:
        """Human decision (maker-checker is enforced by the caller)."""
        new = normalise(target)
        if customer_id not in self._status:
            raise AccountNotFoundError(customer_id)
        await self.writer.flush()  # keep chain order with queued system writes
        async with self.writer.chain.lock, self.db.transaction() as session:
            result = await repo.transition_account_status(
                session,
                customer_id,
                new,
                kind="analyst",
                reason=reason,
                actor=actor,
                expected=(self._status[customer_id], self._version.get(customer_id, 1)),
            )
            if result is None:
                raise AccountNotFoundError(customer_id)
            await self.writer.chain.append_many(
                session,
                [
                    AuditEntry(
                        event_type="ACCOUNT_STATUS",
                        actor=actor,
                        entity_type="account",
                        entity_id=customer_id,
                        customer_id=customer_id,
                        decision=f"{result.previous}->{result.new}",
                        reason=reason,
                    )
                ],
            )
        # cache only after the commit; the analyst decision supersedes a pending one
        self._pending.pop(customer_id, None)
        self._remember(customer_id, result.new, result.version)
        return result.previous, result.new
