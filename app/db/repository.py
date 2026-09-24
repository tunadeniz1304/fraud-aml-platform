"""Query/command helpers over the ORM models (dialect-portable upserts)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import to_money, utcnow
from app.db.models import (
    Account,
    AccountStatusHistory,
    AuditLog,
    Customer,
    Decision,
    GraphRing,
    ModelVersion,
    Transaction,
)


def _insert(session: AsyncSession, model: Any) -> Any:
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    return (postgresql.insert if dialect == "postgresql" else sqlite.insert)(model)


# --- customers / accounts ------------------------------------------------------
async def upsert_customers(session: AsyncSession, records: Iterable[dict[str, Any]]) -> int:
    """Insert/refresh customers and create missing ``AKTIF`` accounts.

    Existing account statuses (e.g. ``BLOKE``) are never overwritten.
    """
    count = 0
    for record in records:
        cid = str(record["customer_id"])
        profile = {
            k: record[k]
            for k in (
                "avg_amount",
                "currency",
                "known_device_ids",
                "known_locations",
                "typical_hours",
                "known_channels",
            )
            if k in record
        }
        values = {
            "id": cid,
            "name": record.get("name", ""),
            "home_city": record.get("home_city", ""),
            "home_country": record.get("home_country", "TR"),
            "birth_year": record.get("birth_year"),
            "vulnerable": bool(record.get("vulnerable", False)),
            "iban": record.get("iban"),
            "segment": record.get("segment", "bireysel"),
            "profile": profile,
            "created_at": utcnow(),
        }
        stmt = _insert(session, Customer).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Customer.id],
            set_={k: stmt.excluded[k] for k in values if k not in ("id", "created_at")},
        )
        await session.execute(stmt)
        acc = _insert(session, Account).values(customer_id=cid, status="AKTIF", updated_at=utcnow())
        await session.execute(acc.on_conflict_do_nothing(index_elements=[Account.customer_id]))
        count += 1
    return count


async def account_statuses(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(select(Account.customer_id, Account.status))
    return {cid: status for cid, status in rows.all()}


async def list_accounts(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Account.customer_id, Customer.name, Account.status, Account.updated_at)
        .join(Customer, Customer.id == Account.customer_id)
        .order_by(Account.customer_id)
    )
    return [
        {"customer_id": cid, "name": name, "hesap_durumu": status, "updated_at": updated}
        for cid, name, status, updated in rows.all()
    ]


async def get_account(session: AsyncSession, customer_id: str) -> dict[str, Any] | None:
    for row in await list_accounts_filtered(session, customer_id):
        return row
    return None


async def list_accounts_filtered(session: AsyncSession, customer_id: str) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Account.customer_id, Customer.name, Account.status, Account.updated_at)
        .join(Customer, Customer.id == Account.customer_id)
        .where(Account.customer_id == customer_id)
    )
    return [
        {"customer_id": cid, "name": name, "hesap_durumu": status, "updated_at": updated}
        for cid, name, status, updated in rows.all()
    ]


async def set_account_status(
    session: AsyncSession,
    customer_id: str,
    new_status: str,
    *,
    previous: str,
    reason: str,
    actor: str,
    transaction_id: str | None = None,
) -> bool:
    result = await session.execute(
        update(Account)
        .where(Account.customer_id == customer_id)
        .values(status=new_status, updated_at=utcnow(), version=Account.version + 1)
    )
    if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
        return False
    session.add(
        AccountStatusHistory(
            customer_id=customer_id,
            from_status=previous,
            to_status=new_status,
            reason=reason,
            actor=actor,
            transaction_id=transaction_id,
        )
    )
    return True


# --- transactions / decisions ---------------------------------------------------
def transaction_values(tx: dict[str, Any]) -> dict[str, Any]:
    ts = tx["ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    session_block = tx.get("session")
    return {
        "id": str(tx["transaction_id"]),
        "ts": ts,
        "customer_id": str(tx["customer_id"]),
        "amount": to_money(tx["amount"]),
        "currency": str(tx.get("currency") or "TRY").upper(),
        "amount_try": to_money(tx.get("amount_try", tx["amount"])),
        "channel": str(tx.get("channel") or ""),
        "device_id": str(tx.get("device_id") or ""),
        "ip_address": str(tx.get("ip_address") or ""),
        "location": str(tx.get("location") or ""),
        "country": str(tx.get("country") or ""),
        "beneficiary_id": str(tx.get("beneficiary_id") or ""),
        "beneficiary_iban": str(tx.get("beneficiary_iban") or ""),
        "beneficiary_name": str(tx.get("beneficiary_name") or ""),
        "purpose": str(tx.get("purpose") or ""),
        "session": session_block if isinstance(session_block, dict) else None,
        "received_at": utcnow(),
    }


async def insert_transactions(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """Idempotent bulk insert (executemany; duplicate ids are ignored)."""
    if rows:
        stmt = _insert(session, Transaction).on_conflict_do_nothing(index_elements=["id"])
        await session.execute(stmt, rows)


async def insert_decisions(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    if rows:
        stmt = _insert(session, Decision).on_conflict_do_nothing(index_elements=["transaction_id"])
        await session.execute(stmt, rows)


async def count(session: AsyncSession, model: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


def audit_query(limit: int) -> Select[tuple[AuditLog]]:
    return select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)


async def list_audit(session: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    rows = (await session.execute(audit_query(limit))).scalars()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "transaction_id": r.transaction_id or r.entity_id,
            "customer_id": r.customer_id or "",
            "risk_score": float(r.risk_score or 0.0),
            "decision": r.decision or r.event_type,
            "reason": r.reason,
            "actor": r.actor,
            "event_type": r.event_type,
            "hash": r.hash,
        }
        for r in rows
    ]


def decimal_to_float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


async def upsert_models(session: AsyncSession, models: Iterable[dict[str, Any]]) -> int:
    """Mirror ``models/registry.json`` into the ``models`` table."""
    count = 0
    for m in models:
        values = {
            "version": str(m["version"]),
            "kind": "lightgbm",
            "status": str(m.get("status", "archived")),
            "path": str(m.get("path", m["version"])),
            "metrics": m.get("metrics") or {},
            "created_at": utcnow(),
        }
        stmt = _insert(session, ModelVersion).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ModelVersion.version],
            set_={k: stmt.excluded[k] for k in ("status", "path", "metrics")},
        )
        await session.execute(stmt)
        count += 1
    return count


async def upsert_rings(session: AsyncSession, rings: Iterable[dict[str, Any]]) -> int:
    count = 0
    for ring in rings:
        stmt = _insert(session, GraphRing).values(
            id=str(ring["id"]), members=ring["members"], stats=ring["stats"], detected_at=utcnow()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[GraphRing.id],
            set_={
                "members": stmt.excluded.members,
                "stats": stmt.excluded.stats,
                "detected_at": stmt.excluded.detected_at,
            },
        )
        await session.execute(stmt)
        count += 1
    return count


async def list_rings(session: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        await session.execute(select(GraphRing).order_by(GraphRing.detected_at.desc()).limit(limit))
    ).scalars()
    return [
        {"id": r.id, "members": r.members, "stats": r.stats, "detected_at": r.detected_at}
        for r in rows
    ]
