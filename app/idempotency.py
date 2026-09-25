"""Idempotency index for ``Pipeline.ingest`` (V6 / B11).

The old ingest path ran a ``transactions ⋈ decisions`` query for **every**
request to find replays persisted before a restart. Under SQLite that query
queued behind the write-behind writer and the case-intake transactions and
was the largest single item of the HTTP latency (see ``docs/PERFORMANCE.md``).

The index answers "could this id already be persisted?" without the database:

* a **Bloom filter** filled at startup with the persisted transaction ids and
  with every id this process decides. A negative answer is exact, so a new id
  (the common case) never touches the database; a positive answer (a real
  replay, or a ~0.1 % false positive) falls back to the stored-decision query.
* with Redis, a **claim** (``SET idem:<id> NX EX``) shared by every worker:
  exactly one worker scores an id, the decision summary is stored under the
  same key so a replay reaching another worker returns the same decision.
  Keys expire after ``idempotency_ttl_s``; older replays are still caught by
  the Bloom filter (startup ids) and the database primary key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from typing import Any

from sqlalchemy import func, select

from app.db.database import Database
from app.db.models import Transaction

logger = logging.getLogger("fraud.idempotency")

PENDING = "__pending__"


class BloomFilter:
    """Fixed-size Bloom filter (double hashing over one BLAKE2b digest)."""

    def __init__(self, capacity: int, error_rate: float = 0.001) -> None:
        capacity = max(1, capacity)
        bits = math.ceil(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        self.size = max(64, bits)
        self.hashes = max(1, round(self.size / capacity * math.log(2)))
        self._bits = bytearray((self.size + 7) // 8)
        self.count = 0

    def _positions(self, key: str) -> list[int]:
        digest = hashlib.blake2b(key.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "little")
        h2 = int.from_bytes(digest[8:], "little") | 1
        return [(h1 + i * h2) % self.size for i in range(self.hashes)]

    def add(self, key: str) -> None:
        for pos in self._positions(key):
            self._bits[pos >> 3] |= 1 << (pos & 7)
        self.count += 1

    def __contains__(self, key: str) -> bool:
        return all(self._bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(key))


class ScalableBloom:
    """Bloom filters chained as they fill (each layer twice the previous), so
    the false-positive rate stays bounded without sizing for the worst case."""

    def __init__(self, capacity: int, error_rate: float = 0.001) -> None:
        self.error_rate = error_rate
        self.layers = [(BloomFilter(capacity, error_rate), capacity)]

    def add(self, key: str) -> None:
        bloom, capacity = self.layers[-1]
        if bloom.count >= capacity:
            capacity *= 2
            bloom = BloomFilter(capacity, self.error_rate)
            self.layers.append((bloom, capacity))
        bloom.add(key)

    def __contains__(self, key: str) -> bool:
        return any(key in bloom for bloom, _ in self.layers)

    @property
    def count(self) -> int:
        return sum(bloom.count for bloom, _ in self.layers)


class IdempotencyIndex:
    """Bloom filter of persisted ids + optional Redis claim (multi-worker)."""

    def __init__(
        self,
        redis: Any = None,
        *,
        ttl_s: int = 86_400,
        capacity: int = 100_000,
        prefix: str = "idem",
    ) -> None:
        self.redis = redis
        self.ttl_s = ttl_s
        self.prefix = prefix
        self.capacity = capacity
        self.bloom = ScalableBloom(capacity)

    async def load(self, db: Database, *, chunk: int = 10_000) -> int:
        """Add every persisted transaction id (streamed, bounded memory)."""
        loaded = 0
        async with db.session() as session:
            total = int(await session.scalar(select(func.count()).select_from(Transaction)) or 0)
            self.bloom = ScalableBloom(max(self.capacity, total * 2))
            result = await session.stream(select(Transaction.id).execution_options(yield_per=chunk))
            async for partition in result.partitions(chunk):
                for (tx_id,) in partition:
                    self.bloom.add(str(tx_id))
                loaded += len(partition)
        logger.info("[Idempotency] %d kalıcı işlem kimliği yüklendi", loaded)
        return loaded

    def add(self, tx_id: str) -> None:
        self.bloom.add(tx_id)

    def maybe_persisted(self, tx_id: str) -> bool:
        return tx_id in self.bloom

    # --- Redis claim (shared by all workers) ------------------------------------
    def _key(self, tx_id: str) -> str:
        return f"{self.prefix}:{tx_id}"

    async def claim(self, tx_id: str) -> bool:
        """True when this worker owns ``tx_id`` (always true without Redis)."""
        if self.redis is None:
            return True
        return bool(await self.redis.set(self._key(tx_id), PENDING, nx=True, ex=self.ttl_s))

    async def record(self, tx_id: str, summary: dict[str, Any]) -> None:
        if self.redis is not None:
            await self.redis.set(self._key(tx_id), json.dumps(summary, default=str), ex=self.ttl_s)

    async def release(self, tx_id: str) -> None:
        """Give up a claim that produced no decision (so a retry may score it)."""
        if self.redis is not None:
            await self.redis.delete(self._key(tx_id))

    async def claimed_result(self, tx_id: str, *, wait_s: float = 30.0) -> dict[str, Any] | None:
        """Decision recorded by the worker that owns ``tx_id`` (waits while pending)."""
        if self.redis is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        delay = 0.002
        while True:
            raw = await self.redis.get(self._key(tx_id))
            if raw is None:
                return None
            if raw != PENDING:
                data: dict[str, Any] = json.loads(raw)
                return data
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.05)
