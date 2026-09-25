"""Short-lived, single-use tickets for Server-Sent Events.

``EventSource`` cannot send an ``Authorization`` header, and a JWT in the
query string leaks into access logs, proxies and browser history. The client
exchanges its token for a random ticket (``POST /api/stream/ticket``) that
is valid for ``sse_ticket_ttl_s`` seconds and **one** connection.

In-process store: SSE connections are served by the worker that issued the
ticket only if the load balancer is sticky; multi-worker deployments without
stickiness should use the Redis variant (not needed for the single-process
demo).
"""

from __future__ import annotations

import secrets
import threading
import time

from app.config import get_settings
from app.security.auth import Principal


class TicketStore:
    def __init__(self, max_entries: int = 10_000) -> None:
        self._tickets: dict[str, tuple[Principal, float]] = {}
        self._lock = threading.Lock()
        self.max_entries = max_entries

    def issue(self, principal: Principal) -> tuple[str, int]:
        ttl = get_settings().sse_ticket_ttl_s
        ticket = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            if len(self._tickets) >= self.max_entries:
                self._tickets = {k: v for k, v in self._tickets.items() if v[1] > now}
            self._tickets[ticket] = (principal, now + ttl)
        return ticket, ttl

    def redeem(self, ticket: str) -> Principal | None:
        with self._lock:
            entry = self._tickets.pop(ticket, None)  # single use
        if entry is None or entry[1] < time.monotonic():
            return None
        return entry[0]


TICKETS = TicketStore()
