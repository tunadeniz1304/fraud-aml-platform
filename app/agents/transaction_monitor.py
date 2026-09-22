"""TransactionMonitor agent.

Listens for raw incoming transfers (``transaction.created``), performs basic
integrity checks, and publishes the monitored event (``transaction.monitored``)
for deeper analysis by the ContextAnalyst.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.event_bus import EventBus

logger = logging.getLogger("fraud.monitor")


class TransactionMonitor:
    """First stage of the pipeline: ingest + basic checks."""

    CREATED = "transaction.created"
    MONITORED = "transaction.monitored"

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.monitored: list[dict[str, Any]] = []
        self.bus.subscribe(self.CREATED, self._on_created)

    def _basic_checks(self, tx: dict[str, Any]) -> list[str]:
        """Return a list of problems (empty when the transfer is well-formed)."""
        problems: list[str] = []
        if not tx.get("transaction_id"):
            problems.append("missing transaction_id")
        if not tx.get("customer_id"):
            problems.append("missing customer_id")
        amount = tx.get("amount")
        if amount is None or amount <= 0:
            problems.append("non-positive amount")
        if not tx.get("device_id"):
            problems.append("missing device_id")
        if not tx.get("location"):
            problems.append("missing location")
        return problems

    async def _on_created(self, tx: dict[str, Any]) -> None:
        problems = self._basic_checks(tx)
        monitored = dict(tx)
        monitored["well_formed"] = not problems
        monitored["issues"] = problems
        self.monitored.append(monitored)
        logger.info(
            "[Monitor] transfer %s aldı -> %s (müşteri: %s, %.2f %s)",
            tx.get("transaction_id"),
            "tamam" if not problems else f"sorunlu {problems}",
            tx.get("customer_id"),
            tx.get("amount", 0.0),
            tx.get("currency", ""),
        )
        await self.bus.publish(self.MONITORED, monitored)
