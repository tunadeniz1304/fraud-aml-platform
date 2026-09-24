"""TransactionMonitor agent.

Listens for raw incoming transfers (``transaction.created``) and performs
integrity checks. Well-formed transfers continue as ``transaction.monitored``;
malformed ones are published as ``transaction.rejected`` and are **never
scored** (fixes bug #7).
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.core.event_bus import EventBus
from app.core.fx import is_supported
from app.monitoring import metrics

logger = logging.getLogger("fraud.monitor")


def _key(tx: dict[str, Any]) -> str | None:
    tx_id = tx.get("transaction_id")
    return str(tx_id) if tx_id else None


class TransactionMonitor:
    """First stage of the pipeline: ingest + basic checks."""

    CREATED = "transaction.created"
    MONITORED = "transaction.monitored"
    REJECTED = "transaction.rejected"

    def __init__(self, bus: EventBus, buffer: int | None = None) -> None:
        self.bus = bus
        size = buffer or get_settings().analyzed_buffer
        self.monitored: deque[dict[str, Any]] = deque(maxlen=size)
        self.rejected: deque[dict[str, Any]] = deque(maxlen=size)
        self.bus.subscribe(self.CREATED, self._on_created)

    @staticmethod
    def _basic_checks(tx: dict[str, Any]) -> list[str]:
        """Return a list of problems (empty when the transfer is well-formed)."""
        problems: list[str] = []
        if not tx.get("transaction_id"):
            problems.append("missing transaction_id")
        if not tx.get("customer_id"):
            problems.append("missing customer_id")
        amount = tx.get("amount")
        try:
            if amount is None or float(amount) <= 0:
                problems.append("non-positive amount")
        except (TypeError, ValueError):
            problems.append("non-numeric amount")
        if not tx.get("device_id"):
            problems.append("missing device_id")
        if not tx.get("location"):
            problems.append("missing location")
        currency = tx.get("currency") or "TRY"
        if not is_supported(str(currency)):
            problems.append(f"unsupported currency {currency}")
        try:
            datetime.fromisoformat(str(tx.get("ts")))
        except (TypeError, ValueError):
            problems.append("invalid ts")
        return problems

    async def _on_created(self, tx: dict[str, Any]) -> None:
        problems = self._basic_checks(tx)
        monitored = dict(tx)
        monitored["well_formed"] = not problems
        monitored["issues"] = problems
        if problems:
            self.rejected.append(monitored)
            metrics.TX_REJECTED.inc()
            logger.warning(
                "[Monitor] transfer %s reddedildi — skorlanmayacak: %s",
                tx.get("transaction_id"),
                problems,
            )
            await self.bus.publish(self.REJECTED, monitored, key=_key(tx))
            return
        self.monitored.append(monitored)
        metrics.TX_TOTAL.labels(source=str(tx.get("ingested_by") or "stream")).inc()
        logger.info(
            "[Monitor] transfer %s aldı -> tamam (müşteri: %s, %s %s)",
            tx.get("transaction_id"),
            tx.get("customer_id"),
            tx.get("amount"),
            tx.get("currency", ""),
        )
        await self.bus.publish(self.MONITORED, monitored, key=_key(tx))
