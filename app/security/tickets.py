"""Short-lived, single-use tickets for Server-Sent Events.

``EventSource`` cannot send an ``Authorization`` header, and a JWT in the
query string leaks into access logs, proxies and browser history. The client
exchanges its token for a random ticket (``POST /api/stream/ticket``) that
is valid for ``sse_ticket_ttl_s`` seconds and **one** connection.

Storage: Redis (``SET ... EX`` + atomic ``GETDEL``) whenever the pipeline has a
Redis client, so a ticket issued by one uvicorn worker is redeemable on any
other (``--workers N`` without sticky sessions). Without Redis the store is
in-process, which is correct for the single-process dev profile only.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any

from app.config import get_settings
from app.security.auth import Principal

_PREFIX = "sse:ticket:"


def _pipeline_redis() -> Any:
    from app.api.state import state

    return getattr(state.pipeline, "redis", None) if state.pipeline is not None else None


class TicketStore:
    def __init__(self, max_entries: int = 10_000, redis: Any = None) -> None:
        self._tickets: dict[str, tuple[Principal, float]] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self._redis = redis

    @property
    def redis(self) -> Any:
        return self._redis if self._redis is not None else _pipeline_redis()

    async def issue(self, principal: Principal) -> tuple[str, int]:
        ttl = get_settings().sse_ticket_ttl_s
        ticket = secrets.token_urlsafe(32)
        redis = self.redis
        if redis is not None:
            body = json.dumps(
                {
                    "username": principal.username,
                    "role": principal.role,
                    "display_name": principal.display_name,
                    "via": principal.via,
                    # kept so a redeemed ticket can be checked against the
                    # jti denylist (logout revokes the SSE ticket too)
                    "token_id": principal.token_id,
                    "expires_at": principal.expires_at,
                }
            )
            await redis.set(_PREFIX + ticket, body, ex=ttl)
            return ticket, ttl
        now = time.monotonic()
        with self._lock:
            if len(self._tickets) >= self.max_entries:
                self._tickets = {k: v for k, v in self._tickets.items() if v[1] > now}
            self._tickets[ticket] = (principal, now + ttl)
        return ticket, ttl

    async def redeem(self, ticket: str) -> Principal | None:
        redis = self.redis
        if redis is not None:
            raw = await redis.getdel(_PREFIX + ticket)  # single use, atomic
            if raw is None:
                return None
            data = json.loads(raw)
            return Principal(
                data["username"],
                data["role"],
                data.get("display_name", ""),
                data.get("via", "jwt"),
                token_id=str(data.get("token_id", "")),
                expires_at=int(data.get("expires_at", 0)),
            )
        with self._lock:
            entry = self._tickets.pop(ticket, None)  # single use
        if entry is None or entry[1] < time.monotonic():
            return None
        return entry[0]


TICKETS = TicketStore()
