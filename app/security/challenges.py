"""One-time step-up (OTP) challenges.

A ``STEP_UP`` decision issues a random challenge id bound to the transaction
and its customer (``step_up_challenge_ttl_s``, default 10 min). The channel
callback ``POST /api/transactions/{id}/step-up-result`` must present that id;
it is consumed atomically (Redis ``GETDEL``), so a result can be reported only
once and only for the transaction/customer it was issued for. Without the
challenge anyone holding an ingest credential could "pass" step-up for any
transaction and teach the profile a fraudster's device and payee.

Lifecycle (A3 / L2):

* **issue** is idempotent per transaction: a replayed ingest (lost response)
  gets the *same* still-valid challenge id back instead of ``None``. The
  challenge key is written before the per-transaction marker, and the marker
  lives only as long as the challenge, so a crash in between or an expired,
  never-used challenge does not block the transaction: a new one is issued.
* **consume** is keyed by ``(transaction, challenge)``: presenting a challenge
  on the wrong transaction does not find (and so does not burn) it.
* **restore** puts a consumed challenge back when the result could not be
  recorded (transaction not persisted yet, database outage), so the channel
  can retry; **mark_resolved** closes the transaction once a result is
  recorded, after which no new challenge is issued for it.

Storage mirrors :mod:`app.security.tickets`: Redis when the pipeline has a
client (shared by every worker), else a bounded (LRU) in-process map.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.security.tickets import _pipeline_redis

_PREFIX = "stepup:challenge:"  # + tx + ":" + challenge id -> customer id
_TX_PREFIX = "stepup:tx:"  # + tx -> challenge id (as long as the challenge lives)
_DONE_PREFIX = "stepup:done:"  # + tx -> "1" once a result was recorded


@dataclass(frozen=True)
class StepUpChallenge:
    challenge_id: str
    transaction_id: str
    customer_id: str


@dataclass
class _Entry:
    challenge: StepUpChallenge
    expires: float
    taken: bool = False


def _key(transaction_id: str, challenge_id: str) -> str:
    return f"{_PREFIX}{transaction_id}:{challenge_id}"


class StepUpChallengeStore:
    def __init__(self, max_entries: int = 100_000, redis: Any = None) -> None:
        self._by_tx: OrderedDict[str, _Entry] = OrderedDict()
        self._done: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self._redis = redis

    @property
    def redis(self) -> Any:
        return self._redis if self._redis is not None else _pipeline_redis()

    def reset(self) -> None:
        """Drop the in-process state (tests; Redis keys expire on their own)."""
        with self._lock:
            self._by_tx.clear()
            self._done.clear()

    @staticmethod
    def _ttl() -> int:
        return int(get_settings().step_up_challenge_ttl_s)

    @staticmethod
    def _done_ttl() -> int:
        return int(get_settings().idempotency_ttl_s)

    def _trim(self, table: OrderedDict[str, Any]) -> None:
        while len(table) > self.max_entries:
            table.popitem(last=False)  # LRU: oldest first, bounded

    async def issue(self, transaction_id: str, customer_id: str) -> StepUpChallenge | None:
        """The challenge of ``transaction_id``: the still-valid one if it was
        already issued (idempotent replay), else a new one. ``None`` when a
        result was already recorded, or the challenge is being redeemed."""
        redis = self.redis
        if redis is not None:
            return await self._issue_redis(redis, transaction_id, customer_id)
        now = time.monotonic()
        with self._lock:
            done = self._done.get(transaction_id)
            if done is not None and done > now:
                return None
            entry = self._by_tx.get(transaction_id)
            if entry is not None and entry.expires > now:
                if entry.taken or entry.challenge.customer_id != customer_id:
                    return None
                self._by_tx.move_to_end(transaction_id)
                return entry.challenge
            challenge = StepUpChallenge(secrets.token_urlsafe(32), transaction_id, customer_id)
            self._by_tx[transaction_id] = _Entry(challenge, now + self._ttl())
            self._by_tx.move_to_end(transaction_id)
            self._trim(self._by_tx)
            return challenge

    async def _issue_redis(
        self, redis: Any, transaction_id: str, customer_id: str, *, attempts: int = 3
    ) -> StepUpChallenge | None:
        ttl = self._ttl()
        for _ in range(attempts):
            if await redis.exists(_DONE_PREFIX + transaction_id):
                return None
            current = await redis.get(_TX_PREFIX + transaction_id)
            if current is not None:
                owner = await redis.get(_key(transaction_id, str(current)))
                if owner is None:  # being redeemed (or consumed and not restored yet)
                    return None
                if str(owner) != customer_id:
                    return None
                return StepUpChallenge(str(current), transaction_id, customer_id)
            challenge_id = secrets.token_urlsafe(32)
            # L2: the challenge first, then the marker — a failure in between
            # leaves no marker, so the transaction is never blocked
            await redis.set(_key(transaction_id, challenge_id), customer_id, ex=ttl)
            if await redis.set(_TX_PREFIX + transaction_id, challenge_id, nx=True, ex=ttl):
                return StepUpChallenge(challenge_id, transaction_id, customer_id)
            await redis.delete(_key(transaction_id, challenge_id))  # lost the race: reuse
        return None

    async def consume(self, challenge_id: str, transaction_id: str) -> StepUpChallenge | None:
        """Atomically redeem the challenge of ``transaction_id`` (single use);
        ``None`` if unknown, expired, or issued for another transaction (which
        is then left untouched)."""
        if not challenge_id or not transaction_id:
            return None
        redis = self.redis
        if redis is not None:
            owner = await redis.getdel(_key(transaction_id, challenge_id))
            if owner is None:
                return None
            return StepUpChallenge(challenge_id, transaction_id, str(owner))
        now = time.monotonic()
        with self._lock:
            entry = self._by_tx.get(transaction_id)
            if (
                entry is None
                or entry.taken
                or entry.expires <= now
                or not hmac.compare_digest(entry.challenge.challenge_id, challenge_id)
            ):
                return None
            entry.taken = True
            return entry.challenge

    async def restore(self, challenge: StepUpChallenge) -> None:
        """Put back a consumed challenge whose result could not be recorded."""
        ttl = self._ttl()
        tx = challenge.transaction_id
        redis = self.redis
        if redis is not None:
            await redis.set(_key(tx, challenge.challenge_id), challenge.customer_id, ex=ttl)
            await redis.set(_TX_PREFIX + tx, challenge.challenge_id, ex=ttl)
            return
        with self._lock:
            self._by_tx[tx] = _Entry(challenge, time.monotonic() + ttl)
            self._trim(self._by_tx)

    async def mark_resolved(self, transaction_id: str) -> None:
        """A result was recorded: no further challenge for this transaction."""
        redis = self.redis
        if redis is not None:
            await redis.set(_DONE_PREFIX + transaction_id, "1", ex=self._done_ttl())
            await redis.delete(_TX_PREFIX + transaction_id)
            return
        with self._lock:
            self._by_tx.pop(transaction_id, None)
            self._done[transaction_id] = time.monotonic() + self._done_ttl()
            self._done.move_to_end(transaction_id)
            self._trim(self._done)


CHALLENGES = StepUpChallengeStore()
