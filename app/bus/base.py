"""Event-bus contract shared by the in-memory and Redis Streams implementations."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

Topic = str
Handler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class RetryLater(Exception):
    """The handler cannot process the message *yet* (e.g. another worker owns
    its idempotency claim). The Redis bus leaves it pending without counting a
    failed delivery; it is redelivered after ``claim_idle_ms``."""


@dataclass(frozen=True)
class DeadLetter:
    """A message whose handler kept failing after all retries."""

    topic: str
    handler: str
    payload: dict[str, Any]
    error: str
    attempts: int
    key: str | None = None
    failed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "handler": self.handler,
            "key": self.key,
            "error": self.error,
            "attempts": self.attempts,
            "failed_at": self.failed_at,
            "payload": self.payload,
        }


def handler_name(handler: Handler) -> str:
    owner = getattr(handler, "__self__", None)
    base = getattr(handler, "__qualname__", None) or repr(handler)
    return f"{type(owner).__name__}.{handler.__name__}" if owner is not None else base


async def invoke(handler: Handler, payload: dict[str, Any]) -> None:
    result = handler(payload)
    if inspect.isawaitable(result):
        await result


class EventBusProtocol(Protocol):
    def subscribe(self, topic: Topic, handler: Handler, **kwargs: Any) -> None: ...

    async def publish(
        self, topic: Topic, payload: dict[str, Any], *, key: str | None = None
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
