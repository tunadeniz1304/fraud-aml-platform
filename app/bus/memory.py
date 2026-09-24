"""In-memory event bus (tests, single-process runs).

Two dispatch modes behind the same ``publish`` call:

* **direct** (default): handlers are awaited in subscription order before
  ``publish`` returns — deterministic, easy to test.
* **partitioned** (``partitions > 0`` after :meth:`start`): events are routed
  by ``partition`` (default: the payload's ``customer_id``) into bounded
  queues, one worker per partition — per-customer ordering like Kafka
  partitions, configurable consumer concurrency and **backpressure**
  (``publish`` waits when a queue is full).

Both modes give every handler ``max_retries`` retries; a message that still
fails is recorded in the **dead-letter queue** and never blocks other
subscribers. With a ``key`` the bus is **idempotent** per (topic, handler,
key): a replayed message is processed at most once.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import zlib
from collections import OrderedDict, defaultdict, deque
from typing import Any

from app.bus.base import DeadLetter, Handler, Topic, handler_name, invoke
from app.monitoring import metrics

logger = logging.getLogger("fraud.eventbus")

_Item = tuple[Topic, dict[str, Any], str | None]
# Set while a partition worker dispatches: nested publishes from handlers are
# dispatched inline (same worker, same ordering) instead of re-queued, which
# would deadlock a full partition waiting on itself.
_IN_WORKER: contextvars.ContextVar[bool] = contextvars.ContextVar("_in_worker", default=False)


class InMemoryBus:
    """A lightweight pub/sub bus backed by asyncio."""

    def __init__(
        self,
        *,
        max_retries: int = 2,
        retry_backoff: float = 0.0,
        partitions: int = 0,
        queue_size: int = 1_000,
        dedupe_size: int = 200_000,
        dlq_size: int = 10_000,
    ) -> None:
        self._subscribers: defaultdict[Topic, list[Handler]] = defaultdict(list)
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.partitions = partitions
        self.queue_size = queue_size
        self._done: OrderedDict[tuple[Topic, int, str], None] = OrderedDict()
        self._dedupe_size = dedupe_size
        self.dlq: deque[DeadLetter] = deque(maxlen=dlq_size)
        self._queues: list[asyncio.Queue[_Item]] = []
        self._workers: list[asyncio.Task[None]] = []
        self.duplicates = 0

    # --- subscription --------------------------------------------------------------
    def subscribe(self, topic: Topic, handler: Handler, **_: Any) -> None:
        """Register ``handler`` for ``topic`` (idempotent)."""
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)

    # --- lifecycle -------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._workers)

    async def start(self) -> None:
        if self.partitions <= 0 or self.running:
            return
        self._queues = [asyncio.Queue(maxsize=self.queue_size) for _ in range(self.partitions)]
        self._workers = [
            asyncio.create_task(self._worker(q), name=f"bus-partition-{i}")
            for i, q in enumerate(self._queues)
        ]

    async def drain(self) -> None:
        for queue in self._queues:
            await queue.join()

    async def stop(self) -> None:
        if not self.running:
            return
        await self.drain()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers, self._queues = [], []

    @property
    def backlog(self) -> int:
        return sum(q.qsize() for q in self._queues)

    # --- publish ----------------------------------------------------------------------
    def _partition(self, partition: str | None) -> int:
        if not partition:
            return 0
        return zlib.crc32(partition.encode("utf-8")) % len(self._queues)

    async def publish(
        self,
        topic: Topic,
        payload: dict[str, Any],
        *,
        key: str | None = None,
        partition: str | None = None,
    ) -> None:
        """Dispatch ``payload`` to every subscriber of ``topic``.

        ``key`` is the idempotency key (e.g. ``transaction_id``); ``partition``
        selects the ordered queue (defaults to ``payload["customer_id"]``).
        """
        if self.running and not _IN_WORKER.get():
            part = partition or str(payload.get("customer_id") or key or "")
            await self._queues[self._partition(part)].put((topic, payload, key))
            metrics.BUS_BACKLOG.set(self.backlog)
            return
        await self._dispatch(topic, payload, key)

    async def _worker(self, queue: asyncio.Queue[_Item]) -> None:
        _IN_WORKER.set(True)
        while True:
            topic, payload, key = await queue.get()
            try:
                await self._dispatch(topic, payload, key)
            finally:
                queue.task_done()

    # --- dispatch ----------------------------------------------------------------------
    def _seen(self, marker: tuple[Topic, int, str]) -> bool:
        if marker in self._done:
            self._done.move_to_end(marker)
            return True
        return False

    def _mark(self, marker: tuple[Topic, int, str]) -> None:
        self._done[marker] = None
        while len(self._done) > self._dedupe_size:
            self._done.popitem(last=False)

    async def _dispatch(self, topic: Topic, payload: dict[str, Any], key: str | None) -> None:
        for index, handler in enumerate(list(self._subscribers.get(topic, ()))):
            marker = (topic, index, key) if key else None
            if marker is not None and self._seen(marker):
                self.duplicates += 1
                metrics.BUS_EVENTS.labels(topic=topic, outcome="duplicate").inc()
                continue
            last_error: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    await invoke(handler, payload)
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001 - isolate subscriber failures
                    last_error = exc
                    if attempt < self.max_retries and self.retry_backoff:
                        await asyncio.sleep(self.retry_backoff * (2**attempt))
            if last_error is None:
                if marker is not None:
                    self._mark(marker)
                metrics.BUS_EVENTS.labels(topic=topic, outcome="ok").inc()
                continue
            letter = DeadLetter(
                topic=topic,
                handler=handler_name(handler),
                payload=payload,
                error=f"{type(last_error).__name__}: {last_error}",
                attempts=self.max_retries + 1,
                key=key,
            )
            self.dlq.append(letter)
            metrics.BUS_EVENTS.labels(topic=topic, outcome="dead_letter").inc()
            metrics.DLQ_SIZE.set(len(self.dlq))
            logger.error(
                "[EventBus] '%s' abonesi %s %d denemede başarısız — DLQ'ya alındı",
                topic,
                letter.handler,
                letter.attempts,
                exc_info=last_error,
            )

    def dlq_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        return [d.as_dict() for d in list(self.dlq)[-limit:]][::-1]
