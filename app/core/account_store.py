"""Relational account + audit store (SQLite, v1).

Thread-safe (bug #15): every statement runs under a re-entrant lock because the
connection is shared (``check_same_thread=False``) between the event loop and
worker threads. Updating a missing account raises
:class:`AccountNotFoundError` instead of silently touching zero rows (bug #8),
and :meth:`escalate` applies the monotonic state machine (bug #4).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import config
from app.core.account_state import Actor, next_status, normalise

logger = logging.getLogger("fraud.store")

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    customer_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    hesap_durumu  TEXT NOT NULL DEFAULT 'AKTIF',
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    risk_score     REAL NOT NULL,
    decision       TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_customer ON audit_log(customer_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
"""


class AccountNotFoundError(KeyError):
    """No ``accounts`` row for the given customer."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class AccountStore:
    """SQLite-backed accounts (with ``hesap_durumu``) and audit log."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or config.get_settings().resolved_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- accounts table ----------------------------------------------------
    def seed_accounts(self, customers_path: Path | None = None) -> int:
        """Create/refresh ``accounts`` rows from ``data/customers.json``.

        Existing ``hesap_durumu`` values (e.g. a previous BLOKE) are preserved.
        """
        path = customers_path or config.get_settings().resolved_customers_path
        with path.open("r", encoding="utf-8") as fh:
            records = json.load(fh)
        now = _utcnow()
        with self._lock:
            for record in records:
                self._conn.execute(
                    "INSERT INTO accounts(customer_id, name, hesap_durumu, updated_at) "
                    "VALUES (?, ?, 'AKTIF', ?) "
                    "ON CONFLICT(customer_id) DO UPDATE SET name=excluded.name, "
                    "updated_at=excluded.updated_at",
                    (record["customer_id"], record.get("name", ""), now),
                )
            self._conn.commit()
        return len(records)

    def get_account(self, customer_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE customer_id = ?", (customer_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_status(self, customer_id: str) -> str | None:
        account = self.get_account(customer_id)
        return account["hesap_durumu"] if account else None

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY customer_id").fetchall()
        return [dict(r) for r in rows]

    def set_hesap_durumu(self, customer_id: str, status: str) -> None:
        """Low-level status write (operator path). Raises if the account is missing."""
        normalized = normalise(status)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE accounts SET hesap_durumu = ?, updated_at = ? WHERE customer_id = ?",
                (normalized, _utcnow(), customer_id),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise AccountNotFoundError(customer_id)
        logger.info("[Store] %s hesap_durumu -> %s", customer_id, normalized)

    def escalate(
        self, customer_id: str, requested: str, *, actor: Actor = "system"
    ) -> tuple[str, str]:
        """Apply the state machine atomically; returns ``(previous, new)``."""
        with self._lock:
            current = self.get_status(customer_id)
            if current is None:
                raise AccountNotFoundError(customer_id)
            target = next_status(current, requested, actor=actor)
            if target != current:
                self.set_hesap_durumu(customer_id, target)
            return current, target

    # --- audit log ---------------------------------------------------------
    def append_audit(
        self,
        *,
        transaction_id: str,
        customer_id: str,
        risk_score: float,
        decision: str,
        reason: str = "",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_log(created_at, transaction_id, customer_id, "
                "risk_score, decision, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (_utcnow(), transaction_id, customer_id, float(risk_score), decision, reason),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
