"""Write-behind queue for the side effects of a decision (V6 / B11).

The HTTP response only needs the score, the decision and the idempotency
record. Case/alert intake, the live (SSE) fan-out and the Redis egress are
handed to this queue and run **after** the response by a single consumer:

* one consumer → side effects keep decision order (case grouping, egress
  order); the audit hash chain keeps its single writer
  (:class:`~app.db.writer.PersistenceWriter`), which this queue feeds;
* bounded queue → a slow database applies **backpressure** to the decision
  path instead of growing memory without limit;
* ``join()`` lets tests, the scenario runner and read-your-writes endpoints
  wait until everything decided so far has been applied;
* without a running consumer (scripts, some tests) every event is applied
  inline, exactly like the writer.

A failing handler is logged and counted, never retried in a loop and never
allowed to stop the consumer (case intake used to swallow its errors too).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.bus.base import handler_name, invoke
from app.monitoring import metrics

logger = logging.getLogger("fraud.writebehind")

SideEffect = Callable[[dict[str, Any]], Awaitable[None] | None]


class WriteBehindQueue:
    def __init__(self, name: str = "decision-effects", *, queue_size: int = 20_000) -> None:
        self.name = name
        self.handlers: list[SideEffect] = []
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self.processed = 0
        self.errors = 0

    def subscribe(self, handler: SideEffect) -> None:
        if handler not in self.handlers:
            self.handlers.append(handler)

    # --- lifecycle ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name=self.name)

    async def join(self) -> None:
        """Wait until every event submitted so far has been applied."""
        if self.running:
            await self._queue.join()

    async def stop(self) -> None:
        if self.running:
            await self.join()
            assert self._task is not None
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    # --- producer / consumer ------------------------------------------------------------
    async def submit(self, event: dict[str, Any]) -> None:
        if not self.running:
            await self._apply(event)
            return
        await self._queue.put(event)  # blocks when full -> backpressure
        metrics.WRITE_BEHIND_BACKLOG.set(self._queue.qsize())

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._apply(event)
            finally:
                self._queue.task_done()
                metrics.WRITE_BEHIND_BACKLOG.set(self._queue.qsize())

    async def _apply(self, event: dict[str, Any]) -> None:
        for handler in list(self.handlers):
            try:
                await invoke(handler, event)
            except Exception:  # side effects must never stop the consumer
                self.errors += 1
                metrics.WRITE_BEHIND_ERRORS.labels(handler=handler_name(handler)).inc()
                logger.exception(
                    "[WriteBehind] %s başarısız (%s)",
                    handler_name(handler),
                    event.get("transaction_id"),
                )
        self.processed += 1
