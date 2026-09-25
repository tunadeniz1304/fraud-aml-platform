"""Sliding-window feature state: in-memory and Redis backends.

Both backends expose the same calls:

* ``snapshot(tx)`` — read the point-in-time state needed by the feature
  functions (history of the last 7 days, payee/device relations, profile);
* ``commit(tx, profile)`` — append the event to every window and persist the
  (possibly updated) profile. **Idempotent per transaction id**: a redelivered
  event (crash after commit, Redis Streams retry) is a no-op, so counters never
  double. The Redis commit is one atomic Lua script;
* ``customer_lock(customer_id)`` — serialises snapshot → score → commit of the
  same customer (asyncio lock in memory, ``SET NX PX`` lock in Redis), so
  concurrent transactions of one customer cannot read the same stale window.
  It **fails closed** (M12): if the Redis lease cannot be obtained within
  ``feature_lock_wait_ms`` (retried every few ms), :class:`FeatureLockTimeout`
  is raised instead of scoring on a possibly stale window; the analyst turns it
  into a ``HOLD`` with reason ``FEATURE_LOCK_TIMEOUT``. The per-process lock
  table is size-capped but never evicts a lock that is held or awaited.

Memory is bounded: windows are trimmed by event time and entity maps are LRU
capped (in-memory) or every key carries a TTL (Redis, ``feature_entity_ttl_days``).
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.config import get_settings
from app.features.types import HistEvent, ProfileState, StateSnapshot, TxView

logger = logging.getLogger("fraud.features")

DAY = 86400.0
WEEK = 7 * DAY
PAIR_TTL_S = 400 * 24 * 3600  # payee relationship memory (≈ 13 months)
PROFILE_TTL_S = 400 * 24 * 3600


class FeatureStateStore(Protocol):
    async def snapshot(self, tx: TxView) -> StateSnapshot: ...

    async def commit(self, tx: TxView, profile: ProfileState) -> None: ...

    async def put_profile(self, customer_id: str, profile: ProfileState) -> None: ...

    def customer_lock(self, customer_id: str) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def close(self) -> None: ...


def _event(tx: TxView) -> HistEvent:
    return HistEvent(
        ts=tx.epoch,
        amount=tx.amount_try,
        beneficiary=tx.beneficiary,
        channel=tx.channel,
        device=tx.device_id,
        hour=tx.ts.hour,
    )


class _LRU(OrderedDict[Any, Any]):
    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap

    def touch(self, key: Any, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.cap:
            self.popitem(last=False)


class FeatureLockTimeout(RuntimeError):
    """The cross-process customer lease could not be obtained in time (M12)."""

    def __init__(self, customer_id: str) -> None:
        super().__init__(f"feature lock timeout for {customer_id}")
        self.customer_id = customer_id


class _LockTable:
    """Per-key asyncio locks, capped like an LRU but never evicting a lock that
    is held or awaited (evicting it would let a second coroutine create a fresh
    lock for the same customer and run concurrently with the holder).

    When every entry is in use the table grows past ``cap`` temporarily and
    shrinks back as locks are released."""

    def __init__(self, cap: int) -> None:
        self.cap = max(1, cap)
        self._entries: OrderedDict[Any, list[Any]] = OrderedDict()  # key -> [lock, users]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def in_use(self, key: Any) -> int:
        entry = self._entries.get(key)
        return int(entry[1]) if entry is not None else 0

    @contextlib.asynccontextmanager
    async def hold(self, key: Any) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = [asyncio.Lock(), 0]
        self._entries.move_to_end(key)
        entry[1] += 1  # counted from the wait on: a waiter pins the lock too
        try:
            self._evict()
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            self._evict()

    def _evict(self) -> None:
        if len(self._entries) <= self.cap:
            return
        for key in list(self._entries):
            if len(self._entries) <= self.cap:
                break
            if self._entries[key][1] == 0:  # oldest idle locks first
                del self._entries[key]


class MemoryFeatureStore:
    """Process-local store (tests, training backfill, single-node runs)."""

    def __init__(self, max_entities: int = 200_000) -> None:
        self.max_entities = max_entities
        self.events: _LRU = _LRU(max_entities)  # cid -> (sorted ts, events)
        self.pairs: _LRU = _LRU(max_entities * 4)  # (cid, payee) -> first seen
        self.payee_first: _LRU = _LRU(max_entities)  # payee -> first seen
        self.payee_senders: _LRU = _LRU(max_entities)  # payee -> {cid: last ts}
        self.device_first: _LRU = _LRU(max_entities)  # device -> first seen
        self.device_customers: _LRU = _LRU(max_entities)  # device -> {cid: last ts}
        self.profiles: _LRU = _LRU(max_entities)  # cid -> ProfileState
        self.committed: _LRU = _LRU(max_entities * 4)  # tx id -> True (idempotency)
        self._locks = _LockTable(max_entities)  # cid -> asyncio.Lock (held ones pinned)

    async def snapshot(self, tx: TxView) -> StateSnapshot:
        now = tx.epoch
        stamps, events = self.events.get(tx.customer_id) or ([], [])
        lo = bisect.bisect_right(stamps, now - WEEK)
        hi = bisect.bisect_right(stamps, now)
        senders = self.payee_senders.get(tx.beneficiary) or {}
        dev_customers = self.device_customers.get(tx.device_id) or {}
        profile = self.profiles.get(tx.customer_id)
        return StateSnapshot(
            history=tuple(events[lo:hi]),
            pair_first_seen=self.pairs.get((tx.customer_id, tx.beneficiary)),
            payee_first_seen=self.payee_first.get(tx.beneficiary) if tx.beneficiary else None,
            payee_senders_24h=frozenset(
                c for c, t in senders.items() if now - DAY < t <= now and c != tx.customer_id
            ),
            device_first_seen=self.device_first.get(tx.device_id) if tx.device_id else None,
            device_customers_7d=frozenset(
                c for c, t in dev_customers.items() if now - WEEK < t <= now and c != tx.customer_id
            ),
            profile=profile.copy() if profile is not None else None,
        )

    @contextlib.asynccontextmanager
    async def customer_lock(self, customer_id: str) -> AsyncIterator[None]:
        async with self._locks.hold(customer_id):
            yield

    async def commit(self, tx: TxView, profile: ProfileState) -> None:
        if tx.transaction_id in self.committed:
            return  # redelivered event: already in every window
        self.committed.touch(tx.transaction_id, True)
        now = tx.epoch
        entry: tuple[list[float], list[HistEvent]] | None = self.events.get(tx.customer_id)
        stamps, events = entry if entry is not None else ([], [])
        pos = bisect.bisect_right(stamps, now)  # keep time order (late events)
        stamps.insert(pos, now)
        events.insert(pos, _event(tx))
        cut = bisect.bisect_right(stamps, stamps[-1] - WEEK)
        if cut:
            del stamps[:cut]
            del events[:cut]
        self.events.touch(tx.customer_id, (stamps, events))
        if tx.beneficiary:
            pair = (tx.customer_id, tx.beneficiary)
            self.pairs.touch(pair, min(self.pairs.get(pair, now), now))
            self.payee_first.touch(
                tx.beneficiary, min(self.payee_first.get(tx.beneficiary, now), now)
            )
            senders = self.payee_senders.get(tx.beneficiary) or {}
            senders[tx.customer_id] = max(now, senders.get(tx.customer_id, now))
            self.payee_senders.touch(
                tx.beneficiary, {c: t for c, t in senders.items() if t > now - DAY}
            )
        if tx.device_id:
            self.device_first.touch(
                tx.device_id, min(self.device_first.get(tx.device_id, now), now)
            )
            customers = self.device_customers.get(tx.device_id) or {}
            customers[tx.customer_id] = max(now, customers.get(tx.customer_id, now))
            self.device_customers.touch(
                tx.device_id, {c: t for c, t in customers.items() if t > now - WEEK}
            )
        self.profiles.touch(tx.customer_id, profile)

    async def put_profile(self, customer_id: str, profile: ProfileState) -> None:
        self.profiles.touch(customer_id, profile)

    async def close(self) -> None:
        return None

    def size(self) -> dict[str, int]:
        return {
            "customers": len(self.events),
            "pairs": len(self.pairs),
            "payees": len(self.payee_first),
            "devices": len(self.device_first),
            "profiles": len(self.profiles),
        }


#: Atomic, idempotent, fenced commit: nothing is written if the caller no longer
#: owns the customer lease (``KEYS[9]``/``ARGV[17]``, returns -1) or the
#: transaction id was already committed (``done`` key); every key gets a TTL.
_COMMIT_LUA = """
if ARGV[17] ~= '' and redis.call('GET', KEYS[9]) ~= ARGV[17] then return -1 end
if redis.call('SET', KEYS[8], '1', 'NX', 'EX', ARGV[16]) == false then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
if ARGV[5] ~= '' then
  redis.call('HSETNX', KEYS[2], ARGV[5], ARGV[1])
  redis.call('EXPIRE', KEYS[2], ARGV[6])
  redis.call('SET', KEYS[3], ARGV[1], 'NX', 'EX', ARGV[7])
  redis.call('ZADD', KEYS[4], ARGV[1], ARGV[8])
  redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', ARGV[9])
  redis.call('EXPIRE', KEYS[4], ARGV[10])
end
if ARGV[11] ~= '' then
  redis.call('SET', KEYS[5], ARGV[1], 'NX', 'EX', ARGV[7])
  redis.call('ZADD', KEYS[6], ARGV[1], ARGV[8])
  redis.call('ZREMRANGEBYSCORE', KEYS[6], '-inf', ARGV[12])
  redis.call('EXPIRE', KEYS[6], ARGV[13])
end
redis.call('SET', KEYS[7], ARGV[14], 'EX', ARGV[15])
return 1
"""
_UNLOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return 0
"""


class RedisFeatureStore:
    """Shared store for multi-process deployments (one round trip per call)."""

    def __init__(self, redis: Any, *, prefix: str = "fs") -> None:
        self.redis = redis
        self.prefix = prefix
        settings = get_settings()
        self.entity_ttl_s = int(settings.feature_entity_ttl_days * DAY)
        self.lock_ttl_ms = settings.feature_lock_ttl_ms
        self.lock_wait_s = settings.feature_lock_wait_ms / 1000
        self.lock_timeouts = 0
        self.lease_lost = 0
        self._tokens: dict[str, str] = {}
        self._local = _LockTable(settings.feature_max_entities)

    def _k(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    @contextlib.asynccontextmanager
    async def customer_lock(self, customer_id: str) -> AsyncIterator[None]:
        """Per-customer lock: an in-process asyncio lock (exact within a worker,
        no polling) plus a cross-process ``SET NX PX`` lease with owner-checked
        release. The lease wait is bounded: after ``feature_lock_wait_ms`` the
        lock fails closed with :class:`FeatureLockTimeout` (M12) — the event is
        never scored against a window another worker may be writing.
        """
        async with self._local.hold(customer_id), self._lease(customer_id):
            yield

    @contextlib.asynccontextmanager
    async def _lease(self, customer_id: str) -> AsyncIterator[None]:
        key, token = self._k("lock", customer_id), uuid.uuid4().hex
        deadline = time.monotonic() + self.lock_wait_s
        acquired = False
        while True:
            acquired = bool(await self.redis.set(key, token, nx=True, px=self.lock_ttl_ms))
            if acquired or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.002)
        if not acquired:
            self.lock_timeouts += 1
            logger.warning(
                "[Features] %s için kilit %d ms içinde alınamadı — güvenli tarafta HOLD",
                customer_id,
                int(self.lock_wait_s * 1000),
            )
            raise FeatureLockTimeout(customer_id)
        # a slow scoring step must not outlive the lease: renew it (owner-checked)
        # every third of the TTL; ``commit`` is fenced on the same token
        self._tokens[customer_id] = token
        renew = asyncio.create_task(self._renew(key, token))
        try:
            yield
        finally:
            renew.cancel()
            self._tokens.pop(customer_id, None)
            with contextlib.suppress(asyncio.CancelledError):
                await renew
            await self.redis.eval(_UNLOCK_LUA, 1, key, token)

    async def _renew(self, key: str, token: str) -> None:
        while True:
            await asyncio.sleep(self.lock_ttl_ms / 3000)
            if not await self.redis.eval(_RENEW_LUA, 1, key, token, self.lock_ttl_ms):
                return

    async def snapshot(self, tx: TxView) -> StateSnapshot:
        now = tx.epoch
        pipe = self.redis.pipeline(transaction=False)
        pipe.zrangebyscore(self._k("ev", tx.customer_id), f"({now - WEEK}", now)
        pipe.hget(self._k("pair", tx.customer_id), tx.beneficiary or "-")
        pipe.get(self._k("pf", tx.beneficiary or "-"))
        pipe.zrangebyscore(self._k("payee_snd", tx.beneficiary or "-"), f"({now - DAY}", now)
        pipe.get(self._k("df", tx.device_id or "-"))
        pipe.zrangebyscore(self._k("dev_cust", tx.device_id or "-"), f"({now - WEEK}", now)
        pipe.get(self._k("prof", tx.customer_id))
        ev, pair, payee_first, senders, dev_first, dev_cust, prof = await pipe.execute()
        history = tuple(HistEvent.from_list(json.loads(m)) for m in ev)
        return StateSnapshot(
            history=history,
            pair_first_seen=float(pair) if pair is not None and tx.beneficiary else None,
            payee_first_seen=float(payee_first)
            if payee_first is not None and tx.beneficiary
            else None,
            payee_senders_24h=frozenset(s for s in senders if s != tx.customer_id)
            if tx.beneficiary
            else frozenset(),
            device_first_seen=float(dev_first) if dev_first is not None and tx.device_id else None,
            device_customers_7d=frozenset(c for c in dev_cust if c != tx.customer_id)
            if tx.device_id
            else frozenset(),
            profile=ProfileState.from_dict(json.loads(prof)) if prof else None,
        )

    async def commit(self, tx: TxView, profile: ProfileState) -> None:
        now = tx.epoch
        member = json.dumps([*_event(tx).as_list(), tx.transaction_id], ensure_ascii=False)
        keys = [
            self._k("ev", tx.customer_id),
            self._k("pair", tx.customer_id),
            self._k("pf", tx.beneficiary or "-"),
            self._k("payee_snd", tx.beneficiary or "-"),
            self._k("df", tx.device_id or "-"),
            self._k("dev_cust", tx.device_id or "-"),
            self._k("prof", tx.customer_id),
            self._k("done", tx.transaction_id),
            self._k("lock", tx.customer_id),
        ]
        args = [
            repr(now),
            member,
            repr(now - WEEK),
            int(WEEK + DAY),
            tx.beneficiary,
            PAIR_TTL_S,
            self.entity_ttl_s,
            tx.customer_id,
            repr(now - DAY),
            int(2 * DAY),
            tx.device_id,
            repr(now - WEEK),
            int(WEEK + DAY),
            json.dumps(profile.to_dict()),
            PROFILE_TTL_S,
            self.entity_ttl_s,
            self._tokens.get(tx.customer_id, ""),
        ]
        if await self.redis.eval(_COMMIT_LUA, len(keys), *keys, *args) == -1:
            self.lease_lost += 1
            logger.warning(
                "[Features] %s kilidi commit öncesi kaybedildi — pencereler yazılmadı",
                tx.customer_id,
            )

    async def put_profile(self, customer_id: str, profile: ProfileState) -> None:
        await self.redis.set(
            self._k("prof", customer_id), json.dumps(profile.to_dict()), ex=PROFILE_TTL_S
        )

    async def close(self) -> None:
        return None
