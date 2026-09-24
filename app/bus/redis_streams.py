"""Redis Streams event bus (production ingress/egress).

* one stream per topic (``{prefix}:{topic}``), capped with ``MAXLEN ~``;
* durable **consumer groups** — each subscriber group reads with
  ``XREADGROUP`` and acknowledges with ``XACK`` only after success;
* failed messages stay pending and are **reclaimed** (``XAUTOCLAIM``) after
  ``claim_idle_ms``; once delivered more than ``max_retries`` times they move
  to the **dead-letter stream** ``{prefix}:dlq`` and are acked;
* **idempotency**: a successfully processed ``key`` (the ``transaction_id``)
  is remembered per group (``SET … EX``), so replays/redeliveries are skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import uuid
from dataclasses import dataclass
from typing import Any

from app.bus.base import Handler, Topic, handler_name, invoke
from app.monitoring import metrics

logger = logging.getLogger("fraud.eventbus.redis")


@dataclass
class _Subscription:
    topic: Topic
    group: str
    handler: Handler


class RedisStreamsBus:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str = "fraud",
        consumer: str | None = None,
        max_retries: int = 3,
        block_ms: int = 500,
        batch: int = 64,
        maxlen: int = 200_000,
        idempotency_ttl: int = 7 * 24 * 3600,
        claim_idle_ms: int = 5_000,
    ) -> None:
        self.redis = redis
        self.prefix = prefix
        self.consumer = consumer or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.max_retries = max_retries
        self.block_ms = block_ms
        self.batch = batch
        self.maxlen = maxlen
        self.idempotency_ttl = idempotency_ttl
        self.claim_idle_ms = claim_idle_ms
        self._subs: list[_Subscription] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self.processed = 0
        self.duplicates = 0

    # --- naming -----------------------------------------------------------------------
    def stream(self, topic: Topic) -> str:
        return f"{self.prefix}:{topic}"

    @property
    def dlq_stream(self) -> str:
        return f"{self.prefix}:dlq"

    def _done_key(self, group: str, key: str) -> str:
        return f"{self.prefix}:done:{group}:{key}"

    # --- api ------------------------------------------------------------------------------
    def subscribe(
        self, topic: Topic, handler: Handler, *, group: str | None = None, **_: Any
    ) -> None:
        self._subs.append(_Subscription(topic, group or handler_name(handler), handler))

    async def publish(
        self, topic: Topic, payload: dict[str, Any], *, key: str | None = None
    ) -> None:
        await self.redis.xadd(
            self.stream(topic),
            {"key": key or "", "payload": json.dumps(payload, ensure_ascii=False, default=str)},
            maxlen=self.maxlen,
            approximate=True,
        )

    async def start(self) -> None:
        self._stopping.clear()
        for sub in self._subs:
            try:
                await self.redis.xgroup_create(
                    self.stream(sub.topic), sub.group, id="0", mkstream=True
                )
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._tasks.append(
                asyncio.create_task(self._consume(sub), name=f"redis-consumer-{sub.group}")
            )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # --- consumer loop -----------------------------------------------------------------------
    async def _consume(self, sub: _Subscription) -> None:
        stream = self.stream(sub.topic)
        while not self._stopping.is_set():
            try:
                await self._reclaim(sub, stream)
                response = await self.redis.xreadgroup(
                    sub.group, self.consumer, {stream: ">"}, count=self.batch, block=self.block_ms
                )
                for _, messages in response or []:
                    for msg_id, fields in messages:
                        await self._handle(sub, stream, msg_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[RedisBus] tüketici hatası (%s)", sub.group)
                await asyncio.sleep(0.5)

    async def _reclaim(self, sub: _Subscription, stream: str) -> None:
        result = await self.redis.xautoclaim(
            stream,
            sub.group,
            self.consumer,
            min_idle_time=self.claim_idle_ms,
            start_id="0-0",
            count=self.batch,
        )
        claimed = result[1] if isinstance(result, list | tuple) and len(result) > 1 else []
        for msg_id, fields in claimed:
            if fields:  # deleted entries come back as None
                await self._handle(sub, stream, msg_id, fields)

    def _attempts_key(self, group: str) -> str:
        return f"{self.prefix}:attempts:{group}"

    async def _record_failure(self, group: str, msg_id: str) -> int:
        """Count failed deliveries ourselves (portable across Redis/fakeredis)."""
        attempts = int(await self.redis.hincrby(self._attempts_key(group), msg_id, 1))
        await self.redis.expire(self._attempts_key(group), self.idempotency_ttl)
        return attempts

    async def _handle(
        self, sub: _Subscription, stream: str, msg_id: str, fields: dict[str, str]
    ) -> None:
        key = fields.get("key") or ""
        if key and await self.redis.exists(self._done_key(sub.group, key)):
            self.duplicates += 1
            metrics.BUS_EVENTS.labels(topic=sub.topic, outcome="duplicate").inc()
            await self.redis.xack(stream, sub.group, msg_id)
            return
        try:
            payload = json.loads(fields.get("payload") or "{}")
            await invoke(sub.handler, payload)
        except Exception as exc:  # noqa: BLE001 - failure path decides retry vs DLQ
            deliveries = await self._record_failure(sub.group, msg_id)
            if deliveries > self.max_retries:
                await self.redis.xadd(
                    self.dlq_stream,
                    {
                        "topic": sub.topic,
                        "group": sub.group,
                        "key": key,
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                        "attempts": str(deliveries),
                        "payload": fields.get("payload", ""),
                        "message_id": msg_id,
                    },
                    maxlen=self.maxlen,
                    approximate=True,
                )
                await self.redis.xack(stream, sub.group, msg_id)
                await self.redis.hdel(self._attempts_key(sub.group), msg_id)
                metrics.BUS_EVENTS.labels(topic=sub.topic, outcome="dead_letter").inc()
                metrics.DLQ_SIZE.set(await self.dlq_size())
                logger.error(
                    "[RedisBus] %s mesajı %d denemede başarısız — DLQ'ya taşındı",
                    msg_id,
                    deliveries,
                )
            else:
                metrics.BUS_EVENTS.labels(topic=sub.topic, outcome="retry").inc()
            return
        pipe = self.redis.pipeline()
        if key:
            pipe.set(self._done_key(sub.group, key), "1", ex=self.idempotency_ttl)
        pipe.xack(stream, sub.group, msg_id)
        pipe.hdel(self._attempts_key(sub.group), msg_id)
        await pipe.execute()
        self.processed += 1
        metrics.BUS_EVENTS.labels(topic=sub.topic, outcome="ok").inc()

    # --- introspection -------------------------------------------------------------------------
    async def dlq_size(self) -> int:
        return int(await self.redis.xlen(self.dlq_stream))

    async def dlq_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.redis.xrevrange(self.dlq_stream, count=limit)
        out = []
        for msg_id, fields in rows:
            entry = dict(fields)
            with contextlib.suppress(json.JSONDecodeError):
                entry["payload"] = json.loads(entry.get("payload") or "{}")
            entry["id"] = msg_id
            out.append(entry)
        return out

    async def pending(self) -> int:
        total = 0
        for sub in self._subs:
            info = await self.redis.xpending(self.stream(sub.topic), sub.group)
            total += int(info.get("pending", 0) if isinstance(info, dict) else info[0])
        return total

    async def undelivered(self) -> int:
        """Groups whose last-delivered id is behind the stream head."""
        behind = 0
        for sub in self._subs:
            stream = self.stream(sub.topic)
            head = await self.redis.xrevrange(stream, count=1)
            if not head:
                continue
            groups = await self.redis.xinfo_groups(stream)
            delivered = next(
                (g.get("last-delivered-id") for g in groups if g.get("name") == sub.group), None
            )
            if delivered != head[0][0]:
                behind += 1
        return behind

    async def drain(self, max_wait: float = 30.0) -> None:
        """Wait until every group has consumed and acked everything (tests/replay)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while loop.time() < deadline:
            if await self.undelivered() == 0 and await self.pending() == 0:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("Redis bus boşaltılamadı")
