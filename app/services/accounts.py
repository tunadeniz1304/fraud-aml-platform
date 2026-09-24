"""Account status service.

The hot path needs the account status *before* scoring (bug #5) without a DB
round trip, so statuses are cached in memory (loaded at start-up) and every
change is persisted through the write-behind writer together with a
``account_status_history`` row and a hash-chained audit entry.

* :meth:`escalate` — system transitions, monotonic (state machine, bug #4);
* :meth:`set_by_analyst` — human decision (de-escalation allowed); written
  synchronously because it is rare and must be durable before returning.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.account_state import Actor, next_status, normalise
from app.db import repository as repo
from app.db.audit import AuditEntry
from app.db.database import Database
from app.db.writer import PersistenceWriter, StatusChange

logger = logging.getLogger("fraud.accounts")


class AccountNotFoundError(KeyError):
    """No account row for the customer."""


class AccountService:
    def __init__(self, db: Database, writer: PersistenceWriter) -> None:
        self.db = db
        self.writer = writer
        self._status: dict[str, str] = {}

    async def load(self) -> int:
        async with self.db.session() as session:
            self._status = await repo.account_statuses(session)
        return len(self._status)

    # --- reads (hot path) -----------------------------------------------------------
    def get_status(self, customer_id: str) -> str | None:
        return self._status.get(customer_id)

    def exists(self, customer_id: str) -> bool:
        return customer_id in self._status

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for status in self._status.values():
            out[status] = out.get(status, 0) + 1
        return out

    async def list_accounts(self) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            return await repo.list_accounts(session)

    async def get_account(self, customer_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            return await repo.get_account(session, customer_id)

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
        """Apply the state machine; persists asynchronously. Returns (prev, new)."""
        current = self._status.get(customer_id)
        if current is None:
            raise AccountNotFoundError(customer_id)
        target = next_status(current, requested, actor=actor)
        if target == current:
            return current, current
        self._status[customer_id] = target
        await self.writer.record_status(
            StatusChange(customer_id, current, target, reason, actor, transaction_id),
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
        current = self._status.get(customer_id)
        if current is None:
            raise AccountNotFoundError(customer_id)
        await self.writer.flush()  # keep chain order with queued system writes
        async with self.writer.chain.lock, self.db.transaction() as session:
            ok = await repo.set_account_status(
                session, customer_id, new, previous=current, reason=reason, actor=actor
            )
            if not ok:
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
                        decision=f"{current}->{new}",
                        reason=reason,
                    )
                ],
            )
        self._status[customer_id] = new
        return current, new
