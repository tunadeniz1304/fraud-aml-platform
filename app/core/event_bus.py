"""Backwards-compatible import path for the in-memory event bus.

The implementation lives in :mod:`app.bus.memory` (retries, dead-letter queue,
idempotency, partitioned queues with backpressure); ``EventBus`` remains the
name used across the agents and tests.
"""

from __future__ import annotations

from app.bus.base import Handler, Topic
from app.bus.memory import InMemoryBus

EventBus = InMemoryBus

__all__ = ["EventBus", "Handler", "InMemoryBus", "Topic"]
