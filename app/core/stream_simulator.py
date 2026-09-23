"""Async transaction stream simulator.

Feeds ``transaction.created`` events onto the bus at a configurable rate with
realistic inter-arrival jitter — a stand-in for a banking Kafka/RabbitMQ feed.
The pipeline consumes the stream in real time; the same agents handle both the
batched ``main.py`` replay and this streaming mode.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
from pathlib import Path
from typing import Any, AsyncIterator

from app import config
from app.agents.transaction_monitor import TransactionMonitor
from app.core.event_bus import EventBus


class TransactionStreamSimulator:
    """Emits historical/mock transfers as an async stream with jitter."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        rate_per_second: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self.path = path or config.DATA_DIR / "transactions.json"
        self.rate_per_second = rate_per_second
        sleep = (1.0 / rate_per_second) if rate_per_second > 0 else 0.0
        self._delay = sleep
        self._rng = random.Random(seed)
        self._transactions = self._load()

    def _load(self) -> list[dict[str, Any]]:
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else [data]

    async def __call__(self, bus: EventBus) -> AsyncIterator[dict[str, Any]]:
        """Yield (and optionally publish) events; yields each payload."""
        for tx in self._transactions:
            packet = copy.deepcopy(tx)
            if self._delay > 0:
                # Jitter: +/- 50% of the mean interval, min 0.
                jitter = self._delay * self._rng.uniform(-0.5, 0.5)
                await asyncio.sleep(max(0.0, self._delay + jitter))
            await bus.publish(TransactionMonitor.CREATED, packet)
            yield packet

    async def run(self, bus: EventBus, *, on_event=None) -> None:
        """Consume the whole stream; ``on_event`` callback for telemetry."""
        async for packet in self(bus):
            if on_event is not None:
                on_event(packet)
