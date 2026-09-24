"""Batch graph analytics from the database (worker job).

Rebuilds the entity graph from the last days of persisted transactions — so
ring detection also covers events processed by other API replicas — runs
Louvain + PageRank and upserts the rings into ``graph_rings``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.db import repository as repo
from app.db.base import utcnow
from app.db.database import Database
from app.db.models import Customer, Label, Transaction
from app.graph.entity_graph import EntityGraph


async def build_from_db(db: Database, *, days: int = 7, limit: int = 200_000) -> EntityGraph:
    graph = EntityGraph()
    since = utcnow() - timedelta(days=days)
    async with db.session() as session:
        for cid, name, iban in (
            await session.execute(select(Customer.id, Customer.name, Customer.iban))
        ).all():
            graph.register_customer(cid, name or "", iban or "")
        rows = (
            await session.execute(
                select(Transaction)
                .where(Transaction.ts >= since)
                .order_by(Transaction.ts)
                .limit(limit)
            )
        ).scalars()
        for tx in rows:
            graph.observe(
                tx_id=tx.id,
                customer_id=tx.customer_id,
                ts=tx.ts.timestamp(),
                amount=float(tx.amount_try),
                beneficiary=tx.beneficiary_iban or tx.beneficiary_id,
                device=tx.device_id,
                ip=tx.ip_address,
            )
        for customer_id, iban, beneficiary_id in (
            await session.execute(
                select(
                    Transaction.customer_id,
                    Transaction.beneficiary_iban,
                    Transaction.beneficiary_id,
                )
                .join(Label, Label.transaction_id == Transaction.id)
                .where(Label.label == 1)
            )
        ).all():
            graph.flag_customer(customer_id)
            graph.flag_account(iban or beneficiary_id)
    return graph


async def detect_and_store(db: Database, *, days: int = 7) -> dict[str, Any]:
    graph = await build_from_db(db, days=days)
    rings = await asyncio.to_thread(graph.detect_rings)
    if rings:
        async with db.transaction() as session:
            await repo.upsert_rings(session, rings)
    return {
        "rings": len(rings),
        **graph.stats(),
        "top": rings[0]["stats"]["summary"] if rings else None,
    }
