"""HTTP hardening: request-id correlation, security headers, CORS, rate limits
and exception sanitising.

* **CSP without ``'unsafe-inline'``** (bug #15): all dashboard JS/CSS is served
  as external static files.
* ``X-Request-ID`` is accepted (sanitised) or generated, bound to every log
  record of the request and echoed in the response.
* Unhandled errors return a generic Turkish message plus the request id —
  internal exception text never leaves the process.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.monitoring import metrics, tracing
from app.security.ratelimit import limiter, rate_limit_handler

logger = logging.getLogger("fraud.http")

STRICT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# Swagger UI (only when API_DOCS=true) needs its CDN bundle.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
    b"cross-origin-opener-policy": b"same-origin",
}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Pure-ASGI middleware (streaming/SSE friendly)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        path: str = scope.get("path", "")
        csp = DOCS_CSP if path.startswith(("/docs", "/redoc")) else STRICT_CSP
        started = time.perf_counter()
        status_holder: dict[str, Any] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw}
                for key, value in _SECURITY_HEADERS.items():
                    if key not in present:
                        raw.append((key, value))
                raw.append((b"content-security-policy", csp.encode()))
                raw.append((b"x-request-id", request_id.encode()))
                message["headers"] = raw
            await send(message)

        structlog.contextvars.bind_contextvars(request_id=request_id)
        method = str(scope.get("method", ""))
        try:
            with tracing.span(f"HTTP {method}", **{"http.target": path, "request_id": request_id}):
                await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            if not path.startswith(("/static", "/assets")) and path != "/metrics":
                # route template (not the raw path) keeps label cardinality bounded
                route = getattr(scope.get("route"), "path", "eşleşmeyen")
                metrics.HTTP_REQUESTS.labels(method, route, str(status_holder["status"])).inc()
                metrics.HTTP_LATENCY.labels(method, route).observe(elapsed / 1000)
                logger.info(
                    "%s %s -> %s (%.1f ms)",
                    method,
                    path,
                    status_holder["status"],
                    elapsed,
                )
            structlog.contextvars.unbind_contextvars("request_id")


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or request.scope.get("state", {}).get(
        "request_id"
    )
    logger.exception("İşlenmeyen hata (request_id=%s)", request_id, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Beklenmeyen sunucu hatası", "request_id": request_id},
    )


def install(app: FastAPI) -> None:
    """Install hardening middleware (outermost last)."""
    settings = get_settings()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
    app.add_exception_handler(Exception, _unhandled)
    app.add_middleware(SlowAPIMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-API-Key"],
        )
    app.add_middleware(RequestContextMiddleware)
