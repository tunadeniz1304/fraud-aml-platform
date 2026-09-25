"""One-time step-up (OTP) challenges.

A ``STEP_UP`` decision issues a random challenge id bound to the transaction
and its customer (``step_up_challenge_ttl_s``, default 10 min). The channel
callback ``POST /api/transactions/{id}/step-up-result`` must present that id;
it is consumed atomically (Redis ``GETDEL``), so a result can be reported only
once and only for the transaction/customer it was issued for. Without the
challenge anyone holding an ingest credential could "pass" step-up for any
transaction and teach the profile a fraudster's device and payee.

Each transaction gets at most one challenge (``SET NX`` marker), so a replayed
ingest cannot mint a fresh one after the first was used.

Storage mirrors :mod:`app.security.tickets`: Redis when the pipeline has a
client (shared by every worker), else a bounded in-process map.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.security.tickets import _pipeline_redis

_PREFIX = "stepup:challenge:"
_TX_PREFIX = "stepup:tx:"
_TX_MARKER_TTL_S = 24 * 3600


@dataclass(frozen=True)
class StepUpChallenge:
    challenge_id: str
    transaction_id: str
    customer_id: str


class StepUpChallengeStore:
    def __init__(self, max_entries: int = 100_000, redis: Any = None) -> None:
        self._challenges: dict[str, tuple[StepUpChallenge, float]] = {}
        self._issued_tx: dict[str, float] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self._redis = redis

    @property
    def redis(self) -> Any:
        return self._redis if self._redis is not None else _pipeline_redis()

    def reset(self) -> None:
        """Drop the in-process state (tests; Redis keys expire on their own)."""
        with self._lock:
            self._challenges.clear()
            self._issued_tx.clear()

    async def issue(self, transaction_id: str, customer_id: str) -> StepUpChallenge | None:
        """New challenge for ``transaction_id``; ``None`` if one was already issued."""
        ttl = get_settings().step_up_challenge_ttl_s
        challenge = StepUpChallenge(secrets.token_urlsafe(32), transaction_id, customer_id)
        redis = self.redis
        if redis is not None:
            first = await redis.set(_TX_PREFIX + transaction_id, "1", nx=True, ex=_TX_MARKER_TTL_S)
            if not first:
                return None
            body = json.dumps({"tx": transaction_id, "customer": customer_id})
            await redis.set(_PREFIX + challenge.challenge_id, body, ex=ttl)
            return challenge
        now = time.monotonic()
        with self._lock:
            if len(self._issued_tx) >= self.max_entries:
                self._issued_tx = {k: v for k, v in self._issued_tx.items() if v > now}
            if self._issued_tx.get(transaction_id, 0.0) > now:
                return None
            self._issued_tx[transaction_id] = now + _TX_MARKER_TTL_S
            if len(self._challenges) >= self.max_entries:
                self._challenges = {k: v for k, v in self._challenges.items() if v[1] > now}
            self._challenges[challenge.challenge_id] = (challenge, now + ttl)
        return challenge

    async def consume(self, challenge_id: str) -> StepUpChallenge | None:
        """Atomically redeem a challenge (single use); ``None`` if unknown/expired."""
        if not challenge_id:
            return None
        redis = self.redis
        if redis is not None:
            raw = await redis.getdel(_PREFIX + challenge_id)
            if raw is None:
                return None
            data = json.loads(raw)
            return StepUpChallenge(challenge_id, str(data["tx"]), str(data["customer"]))
        with self._lock:
            entry = self._challenges.pop(challenge_id, None)
        if entry is None or entry[1] < time.monotonic():
            return None
        return entry[0]


CHALLENGES = StepUpChallengeStore()
