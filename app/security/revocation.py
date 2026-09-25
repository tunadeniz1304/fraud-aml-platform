"""Access-token revocation (logout).

Every JWT carries a random ``jti``. ``POST /api/auth/logout`` puts it on a
denylist until the token would have expired anyway, so a stolen or shared
token stops working as soon as its owner logs out.

Storage mirrors :mod:`app.security.tickets`: Redis when the pipeline has a
client (shared by every worker), else a bounded in-process map.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from app.security.tickets import _pipeline_redis

_PREFIX = "auth:revoked:"


class TokenDenylist:
    def __init__(self, max_entries: int = 100_000, redis: Any = None) -> None:
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self._redis = redis

    @property
    def redis(self) -> Any:
        return self._redis if self._redis is not None else _pipeline_redis()

    def reset(self) -> None:
        with self._lock:
            self._revoked.clear()

    async def revoke(self, token_id: str, expires_at: int) -> None:
        """Deny ``token_id`` until ``expires_at`` (unix seconds)."""
        if not token_id:
            return
        ttl = max(1, int(expires_at - time.time()))
        redis = self.redis
        if redis is not None:
            await redis.set(_PREFIX + token_id, "1", ex=ttl)
            return
        now = time.time()
        with self._lock:
            if len(self._revoked) >= self.max_entries:
                self._revoked = {k: v for k, v in self._revoked.items() if v > now}
            self._revoked[token_id] = now + ttl

    async def is_revoked(self, token_id: str) -> bool:
        if not token_id:
            return False
        redis = self.redis
        if redis is not None:
            return bool(await redis.exists(_PREFIX + token_id))
        with self._lock:
            return self._revoked.get(token_id, 0.0) > time.time()


REVOKED = TokenDenylist()
