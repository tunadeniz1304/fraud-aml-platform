"""Relational account + audit store (SQLite).

Adım 5 deliverable: the action engine must update the customer's
``hesap_durumu`` column to ``BLOKE`` when an autonomous block is placed, and
append a durable audit log row. SQLite is the boring, zero-config choice for
this local simulation; a Kafka/RabbitMQ + Postgres deployment would swap this
module for real adapters behind the same interface.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import config

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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountStore:
    """SQLite-backed accounts (with ``hesap_durumu``) and audit log."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- accounts table ----------------------------------------------------
    def seed_accounts(
        self, customers_path: Path | None = None
    ) -> int:
        """Create/refresh ``accounts`` rows from ``data/customers.json``.

        Returns number of rows (re)written. Existing ``hesap_durumu`` values
        (e.g. a previously placed BLOKE) are preserved.
        """
        path = customers_path or config.DATA_DIR / "customers.json"
        with path.open("r", encoding="utf-8") as fh:
            records = json.load(fh)
        now = _utcnow()
        for record in records:
            cid = record["customer_id"]
            current = self._conn.execute(
                "SELECT hesap_durumu FROM accounts WHERE customer_id = ?", (cid,)
            ).fetchone()
            status = current["hesap_durumu"] if current else "AKTIF"
            self._conn.execute(
                "INSERT INTO accounts(customer_id, name, hesap_durumu, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(customer_id) DO UPDATE SET name=excluded.name, "
                "updated_at=excluded.updated_at",
                (cid, record.get("name", ""), status, now),
            )
        self._conn.commit()
        return len(records)

    def get_account(self, customer_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE customer_id = ?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM accounts ORDER BY customer_id")]

    def set_hesap_durumu(self, customer_id: str, status: str) -> None:
        """Transition the account to a new status (e.g. ``BLOKE``)."""
        normalized = status.strip().upper()
        if normalized not in ("AKTIF", "BLOKE", "INCELENIYOR"):
            raise ValueError(f"Geçersiz hesap_durumu: {status!r}")
        self._conn.execute(
            "UPDATE accounts SET hesap_durumu = ?, updated_at = ? WHERE customer_id = ?",
            (normalized, _utcnow(), customer_id),
        )
        self._conn.commit()
        logger.info("[Store] %s hesap_durumu -> %s", customer_id, normalized)

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
        cur = self._conn.execute(
            "INSERT INTO audit_log(created_at, transaction_id, customer_id, "
            "risk_score, decision, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (_utcnow(), transaction_id, customer_id, float(risk_score), decision, reason),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]
