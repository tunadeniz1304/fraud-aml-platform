"""Pub/sub asyncio event bus for the fraud detection pipeline.

Agents subscribe to the event topics they care about and other components
publish events onto the bus, decoupling producers from consumers. Processing
is awaited sequentially per event so the simulation remains deterministic and
easy to reason about while still being fully event-driven.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from typing import Any, Awaitable, Callable, DefaultDict

Topic = str
# A subscriber callback may be sync or async.
Handler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class EventBus:
    """A lightweight in-memory pub/sub bus backed by asyncio."""

    def __init__(self) -> None:
        self._subscribers: DefaultDict[Topic, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: Topic, handler: Handler) -> None:
        """Register ``handler`` to be invoked for every event on ``topic``."""
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)

    async def publish(self, topic: Topic, payload: dict[str, Any]) -> None:
        """Dispatch ``payload`` to every subscriber of ``topic`` in order."""
        for handler in list(self._subscribers.get(topic, ())):
            result = handler(payload)
            if inspect.isawaitable(result):
                await result
