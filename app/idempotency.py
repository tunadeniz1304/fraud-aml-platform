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
* with Redis, a **claim** (``SET idem:<id> NX PX``) shared by every worker:
  exactly one worker scores an id, the decision summary is stored under the
  same key so a replay reaching another worker returns the same decision.
  Keys expire after ``idempotency_ttl_s``; older replays are still caught by
  the Bloom filter (startup ids) and the database primary key.

M1 hardening:

* every claim and summary carries the **payload digest** (SHA-256 of the
  canonical request); the same id with a different payload is rejected with
  :class:`IdempotencyConflict` (HTTP 409) instead of returning the decision
  of another transaction;
* a PENDING claim lives only ``pending_ttl_s`` (default 45 s) and is
  refreshed by its owner while it works, so a crashed worker blocks the id
  for at most one lease instead of the whole idempotency window;
* a concurrent retry waits ``pending_wait_s`` (default 250 ms) and then gets
  :class:`IngestInProgress` (HTTP 409 "processing" + ``Retry-After``) instead
  of blocking for 30 s and re-scoring;
* the owner first stores a **provisional** summary (still on the short,
  refreshed lease) and marks it committed only once the persistence writer
  committed the decision. The Redis ingress requires a committed summary, so
  a crash between the decision and the commit ends in a redelivery that
  scores the id again instead of an acknowledged-but-lost transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.bus.base import RetryLater
from app.db.database import Database
from app.db.models import Transaction

logger = logging.getLogger("fraud.idempotency")

PENDING = "__pending__"
#: request fields that do not change the transaction itself
_VOLATILE = frozenset({"ingested_by", "payload_hash"})

# refresh a lease only while it still holds *our* value
_REFRESH = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
# delete a key only while it still holds *our* value
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class IdempotencyConflict(Exception):
    """The id was already used for a different payload (HTTP 409)."""

    def __init__(self, tx_id: str) -> None:
        super().__init__(f"transaction_id {tx_id} başka bir içerikle zaten kullanıldı")
        self.tx_id = tx_id


class IngestInProgress(RetryLater):
    """Another request / worker is still scoring this id (HTTP 409 + Retry-After)."""

    def __init__(self, tx_id: str, retry_after_s: float = 1.0) -> None:
        super().__init__(f"transaction_id {tx_id} hâlâ işleniyor")
        self.tx_id = tx_id
        self.retry_after_s = retry_after_s


def _canonical_ts(value: Any) -> Any:
    """L9: one instant, one digest -- an offset-aware ``ts`` is rendered in UTC
    (``...+03:00`` and the same instant in ``+00:00`` hash alike). A naive
    or unparsable value is kept as sent (stored digests stay valid)."""
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed is None or parsed.tzinfo is None:
        return value if not isinstance(value, datetime) else value.isoformat()
    return parsed.astimezone(UTC).isoformat()


def payload_digest(tx: dict[str, Any]) -> str:
    """SHA-256 of the canonical request (sorted keys, sender fields excluded,
    an offset-aware ``ts`` normalised to UTC)."""
    doc = {k: v for k, v in tx.items() if k not in _VOLATILE and v is not None}
    if "ts" in doc:
        doc["ts"] = _canonical_ts(doc["ts"])
    return _sha(doc)


def legacy_payload_digest(tx: dict[str, Any]) -> str:
    """Digest as computed before L9 (``ts`` as sent); matches the
    ``payload_hash`` of rows persisted before the UTC normalisation."""
    return _sha({k: v for k, v in tx.items() if k not in _VOLATILE and v is not None})


def _sha(doc: dict[str, Any]) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_digest(
    tx_id: str, result: dict[str, Any] | None, digest: str, *, legacy: str = ""
) -> None:
    """Raise :class:`IdempotencyConflict` when ``result`` belongs to another payload.

    ``legacy`` (the pre-L9 digest of the same request) is also accepted, so a
    replay of a row stored before the ``ts`` normalisation is not a 409.
    """
    stored = (result or {}).get("payload_hash")
    if stored and digest and stored != digest and stored != legacy:
        raise IdempotencyConflict(tx_id)


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
        pending_ttl_s: float = 45.0,
        pending_wait_s: float = 0.25,
    ) -> None:
        self.redis = redis
        self.ttl_s = ttl_s
        self.prefix = prefix
        self.capacity = capacity
        self.pending_ttl_ms = max(100, int(pending_ttl_s * 1000))
        self.pending_wait_s = pending_wait_s
        self.bloom = ScalableBloom(capacity)
        #: keys this process owns -> the value it stored (lease refreshed)
        self._owned: dict[str, str] = {}
        self._refresher: asyncio.Task[None] | None = None

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

    @property
    def retry_after_s(self) -> float:
        return max(1.0, self.pending_ttl_ms / 3000)

    def _own(self, tx_id: str, value: str) -> None:
        self._owned[tx_id] = value
        if self._refresher is None or self._refresher.done():
            self._refresher = asyncio.create_task(self._refresh_loop(), name="idem-lease")

    async def _refresh_loop(self) -> None:
        """Keep the leases of in-flight ids alive (exits when none are left)."""
        interval = self.pending_ttl_ms / 3000
        while self._owned:
            await asyncio.sleep(interval)
            for tx_id, value in list(self._owned.items()):
                try:
                    await self.redis.eval(_REFRESH, 1, self._key(tx_id), value, self.pending_ttl_ms)
                except Exception:  # noqa: BLE001 - next tick retries; the lease may lapse
                    logger.warning("[Idempotency] %s kilidi yenilenemedi", tx_id)

    async def close(self) -> None:
        """Stop refreshing; the leases of unfinished ids then expire on their own."""
        self._owned.clear()
        if self._refresher is not None:
            self._refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresher
            self._refresher = None

    async def claim(self, tx_id: str, digest: str = "") -> bool:
        """True when this worker owns ``tx_id`` (always true without Redis)."""
        if self.redis is None:
            return True
        # the owner token makes release / refresh compare-and-set per claim
        value = f"{PENDING}:{digest}:{uuid.uuid4().hex[:12]}"
        if not await self.redis.set(self._key(tx_id), value, nx=True, px=self.pending_ttl_ms):
            return False
        self._own(tx_id, value)
        return True

    async def record(
        self,
        tx_id: str,
        summary: dict[str, Any],
        digest: str = "",
        *,
        committed: bool = True,
    ) -> None:
        """Store the decision of ``tx_id``.

        A provisional summary (``committed=False``: decided, not yet in the
        database) keeps the short, refreshed lease until :meth:`commit`.
        """
        if self.redis is None:
            return
        doc = {**summary, "_digest": digest, "_committed": committed}
        if not committed:
            doc["_owner"] = uuid.uuid4().hex[:12]
        value = json.dumps(doc, default=str)
        if committed:
            self._owned.pop(tx_id, None)
            await self.redis.set(self._key(tx_id), value, ex=self.ttl_s)
        else:
            await self.redis.set(self._key(tx_id), value, px=self.pending_ttl_ms)
            self._own(tx_id, value)

    async def commit(self, tx_id: str) -> None:
        """The decision of ``tx_id`` is in the database: keep it for the full TTL."""
        value = self._owned.pop(tx_id, None)
        if self.redis is None or value is None or value.startswith(PENDING):
            return
        doc = {**json.loads(value), "_committed": True}
        doc.pop("_owner", None)
        committed = json.dumps(doc, default=str)
        await self.redis.set(self._key(tx_id), committed, ex=self.ttl_s)

    def disown(self, tx_id: str) -> None:
        """Stop refreshing the claim of ``tx_id`` without deleting it (L1).

        The decision may still land in this process; the lease lapses after
        ``pending_ttl_s`` and a redelivery then takes the id over and checks
        the database before scoring it again.
        """
        self._owned.pop(tx_id, None)

    async def release(self, tx_id: str) -> None:
        """Give up a claim that produced no decision (so a retry may score it).

        Compare-and-delete: a claim that expired and was taken over by another
        worker is left alone.
        """
        value = self._owned.pop(tx_id, None)
        if self.redis is not None and value is not None:
            await self.redis.eval(_RELEASE, 1, self._key(tx_id), value)

    async def claimed_result(
        self,
        tx_id: str,
        digest: str = "",
        *,
        wait_s: float | None = None,
        require_committed: bool = False,
    ) -> dict[str, Any] | None:
        """Decision recorded by the worker that owns ``tx_id``.

        Waits up to ``wait_s`` (default ``pending_wait_s``) while the owner is
        still working, then raises :class:`IngestInProgress`. ``None`` means
        the claim is gone (released or expired): the caller may claim the id
        itself. A digest mismatch raises :class:`IdempotencyConflict`.
        """
        if self.redis is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.pending_wait_s if wait_s is None else wait_s)
        delay = 0.002
        while True:
            raw = await self.redis.get(self._key(tx_id))
            if raw is None:
                return None
            if raw.startswith(PENDING):
                owner = raw[len(PENDING) + 1 :].split(":", 1)[0]
                if digest and owner and owner != digest:
                    raise IdempotencyConflict(tx_id)
            else:
                data: dict[str, Any] = json.loads(raw)
                stored = data.pop("_digest", "")
                committed = data.pop("_committed", True)
                data.pop("_owner", None)
                if digest and stored and stored != digest:
                    raise IdempotencyConflict(tx_id)
                if committed or not require_committed:
                    return data
            if loop.time() >= deadline:
                raise IngestInProgress(tx_id, retry_after_s=self.retry_after_s)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.05)
