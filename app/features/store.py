"""Sliding-window feature state: in-memory and Redis backends.

Both backends expose the same two calls:

* ``snapshot(tx)`` — read the point-in-time state needed by the feature
  functions (history of the last 7 days, payee/device relations, profile);
* ``commit(tx, profile)`` — append the event to every window and persist the
  (possibly updated) profile.

Memory is bounded: windows are trimmed by event time and entity maps are LRU
capped (in-memory) or carry TTLs (Redis).
"""

from __future__ import annotations

import bisect
import json
from collections import OrderedDict
from typing import Any, Protocol

from app.features.types import HistEvent, ProfileState, StateSnapshot, TxView

DAY = 86400.0
WEEK = 7 * DAY
PAIR_TTL_S = 400 * 24 * 3600  # payee relationship memory (≈ 13 months)
PROFILE_TTL_S = 400 * 24 * 3600


class FeatureStateStore(Protocol):
    async def snapshot(self, tx: TxView) -> StateSnapshot: ...

    async def commit(self, tx: TxView, profile: ProfileState) -> None: ...

    async def put_profile(self, customer_id: str, profile: ProfileState) -> None: ...

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

    async def commit(self, tx: TxView, profile: ProfileState) -> None:
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


class RedisFeatureStore:
    """Shared store for multi-process deployments (one round trip per call)."""

    def __init__(self, redis: Any, *, prefix: str = "fs") -> None:
        self.redis = redis
        self.prefix = prefix

    def _k(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    async def snapshot(self, tx: TxView) -> StateSnapshot:
        now = tx.epoch
        pipe = self.redis.pipeline(transaction=False)
        pipe.zrangebyscore(self._k("ev", tx.customer_id), f"({now - WEEK}", now)
        pipe.hget(self._k("pair", tx.customer_id), tx.beneficiary or "-")
        pipe.hget(self._k("payee_first"), tx.beneficiary or "-")
        pipe.zrangebyscore(self._k("payee_snd", tx.beneficiary or "-"), f"({now - DAY}", now)
        pipe.hget(self._k("dev_first"), tx.device_id or "-")
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
        pipe = self.redis.pipeline(transaction=False)
        ev_key = self._k("ev", tx.customer_id)
        member = json.dumps([*_event(tx).as_list(), tx.transaction_id], ensure_ascii=False)
        pipe.zadd(ev_key, {member: now})
        pipe.zremrangebyscore(ev_key, "-inf", now - WEEK)
        pipe.expire(ev_key, int(WEEK + DAY))
        if tx.beneficiary:
            pipe.hsetnx(self._k("pair", tx.customer_id), tx.beneficiary, now)
            pipe.expire(self._k("pair", tx.customer_id), PAIR_TTL_S)
            pipe.hsetnx(self._k("payee_first"), tx.beneficiary, now)
            snd = self._k("payee_snd", tx.beneficiary)
            pipe.zadd(snd, {tx.customer_id: now})
            pipe.zremrangebyscore(snd, "-inf", now - DAY)
            pipe.expire(snd, int(2 * DAY))
        if tx.device_id:
            pipe.hsetnx(self._k("dev_first"), tx.device_id, now)
            dc = self._k("dev_cust", tx.device_id)
            pipe.zadd(dc, {tx.customer_id: now})
            pipe.zremrangebyscore(dc, "-inf", now - WEEK)
            pipe.expire(dc, int(WEEK + DAY))
        pipe.set(self._k("prof", tx.customer_id), json.dumps(profile.to_dict()), ex=PROFILE_TTL_S)
        await pipe.execute()

    async def put_profile(self, customer_id: str, profile: ProfileState) -> None:
        await self.redis.set(
            self._k("prof", customer_id), json.dumps(profile.to_dict()), ex=PROFILE_TTL_S
        )

    async def close(self) -> None:
        return None
