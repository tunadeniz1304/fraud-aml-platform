"""Hash-chained, tamper-evident audit log.

Each row stores ``prev_hash`` and ``hash = SHA-256(prev_hash || canonical(row))``
(HMAC-SHA256 keyed with ``AUDIT_HMAC_KEY`` when that is set) where
``canonical`` is a sorted, compact JSON rendering of every business field.
Changing, deleting or reordering any historical row breaks the chain from
that point on, which :func:`verify_chain` reports.

Appends are serialised: an ``asyncio.Lock`` inside the process and, on
PostgreSQL, a transaction-scoped advisory lock across processes (API + worker).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import AuditLog
from app.monitoring.metrics import AUDIT_VERIFY_FAILURES

GENESIS = "0" * 64
_ADVISORY_KEY = 7_240_531  # arbitrary, stable


@dataclass
class AuditEntry:
    event_type: str
    reason: str = ""
    actor: str = "system"
    entity_type: str = "transaction"
    entity_id: str = ""
    transaction_id: str | None = None
    customer_id: str | None = None
    risk_score: float | None = None
    decision: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)


def canonical(entry: AuditEntry | AuditLog) -> str:
    created = entry.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    risk = entry.risk_score
    doc = {
        "created_at": created.astimezone(UTC).isoformat(timespec="microseconds"),
        "actor": entry.actor,
        "event_type": entry.event_type,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "transaction_id": entry.transaction_id,
        "customer_id": entry.customer_id,
        "risk_score": None if risk is None else f"{float(risk):.6f}",
        "decision": entry.decision,
        "reason": entry.reason,
        "payload": entry.payload or {},
    }
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def chain_hash(prev_hash: str, entry: AuditEntry | AuditLog) -> str:
    message = (prev_hash + canonical(entry)).encode("utf-8")
    key = get_settings().audit_hmac_key.get_secret_value()
    if key:
        return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


class AuditChain:
    """Serialised appender for the audit hash chain.

    Callers must hold :attr:`lock` for the **whole** DB transaction that calls
    :meth:`append_many` (until commit); otherwise two writers could read the
    same head hash and fork the chain.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()

    @staticmethod
    async def _last_hash(session: AsyncSession) -> str:
        row = await session.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))
        return row.scalar_one_or_none() or GENESIS

    async def append_many(
        self, session: AsyncSession, entries: list[AuditEntry]
    ) -> list[dict[str, Any]]:
        """Append ``entries`` inside the caller's transaction (in order)."""
        if not entries:
            return []
        if not self.lock.locked():  # pragma: no cover - programming error guard
            raise RuntimeError("AuditChain.append_many kilit tutulmadan çağrıldı")
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            # Cross-process serialisation (API + worker); released at commit.
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_KEY})
        prev = await self._last_hash(session)
        rows: list[dict[str, Any]] = []
        for entry in entries:
            digest = chain_hash(prev, entry)
            rows.append(
                {
                    "created_at": entry.created_at,
                    "actor": entry.actor,
                    "event_type": entry.event_type,
                    "entity_type": entry.entity_type,
                    "entity_id": entry.entity_id,
                    "transaction_id": entry.transaction_id,
                    "customer_id": entry.customer_id,
                    "risk_score": entry.risk_score,
                    "decision": entry.decision,
                    "reason": entry.reason,
                    "payload": entry.payload or {},
                    "prev_hash": prev,
                    "hash": digest,
                }
            )
            prev = digest
        # executemany keeps insertion (= id) order, which is the chain order.
        await session.execute(insert(AuditLog), rows)
        return rows

    async def append(self, db: Any, entries: list[AuditEntry]) -> None:
        """Convenience: own transaction + lock (for rare, synchronous writes)."""
        async with self.lock, db.transaction() as session:
            await self.append_many(session, entries)


@dataclass
class VerifyResult:
    ok: bool
    checked: int
    first_broken_id: int | None = None
    detail: str = ""
    head_hash: str = GENESIS

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "first_broken_id": self.first_broken_id,
            "detail": self.detail,
            "head_hash": self.head_hash,
        }


async def verify_chain(session: AsyncSession, *, batch: int = 2000) -> VerifyResult:
    """Recompute the whole chain in id order (streamed in batches)."""
    prev = GENESIS
    checked = 0
    last_id = 0
    while True:
        result = await session.execute(
            select(AuditLog).where(AuditLog.id > last_id).order_by(AuditLog.id).limit(batch)
        )
        rows = list(result.scalars())
        if not rows:
            break
        for row in rows:
            if row.prev_hash != prev:
                AUDIT_VERIFY_FAILURES.inc()
                return VerifyResult(False, checked, row.id, "prev_hash zinciri kopuk", prev)
            if not hmac.compare_digest(chain_hash(prev, row), row.hash):
                AUDIT_VERIFY_FAILURES.inc()
                return VerifyResult(False, checked, row.id, "satır içeriği değiştirilmiş", prev)
            prev = row.hash
            checked += 1
            last_id = row.id
    return VerifyResult(True, checked, None, "zincir bütün", prev)
