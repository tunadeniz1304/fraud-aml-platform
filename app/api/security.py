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
from app.security.auth import (
    AuthError,
    admin_token,
    constant_time_equals,
    decode_token,
    service_api_key,
    signed_by_us,
)
from app.security.ratelimit import (
    auth_failures_exceeded,
    limiter,
    rate_limit_handler,
    record_auth_failure,
)
from app.security.revocation import REVOKED

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
# HTTPS only (prod sits behind TLS); never sent outside prod so a plain-HTTP
# dev server is not pinned in the browser.
_HSTS = b"max-age=31536000; includeSubDomains"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _throttle_exempt(path: str) -> bool:
    """Probes and the scrape endpoint are never throttled: an orchestrator or
    Prometheus that shares a NAT address with a guessing client must keep
    seeing the service."""
    return path in ("/health", "/api/health", "/metrics") or path.startswith("/api/health/")


def _bearer_value(headers: dict[bytes, bytes]) -> str:
    scheme, _, value = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _is_guess(headers: dict[bytes, bytes], method: str, path: str) -> bool:
    """Does a 401 for this request look like credential guessing?

    Only requests that tried to authenticate with something this server did
    not issue count: an unknown bearer / API key / HMAC signature, or a failed
    password login. A request without credentials (a dashboard tab whose
    session ended) or with a token we signed ourselves (expired / revoked
    session) is not a guess and must not push a shared NAT address into the
    block.
    """
    token = _bearer_value(headers)
    if token:
        return not signed_by_us(token)
    if headers.get(b"x-api-key") or headers.get(b"x-signature"):
        return True
    return method == "POST" and path == "/api/auth/login"


async def _has_valid_credentials(headers: dict[bytes, bytes]) -> bool:
    """A request that authenticates on its own is never blocked by the
    per-address failure throttle (someone else behind the NAT guessed)."""
    token = _bearer_value(headers)
    if token:
        configured = admin_token()
        if configured and constant_time_equals(token, configured):
            return True
        try:
            principal = decode_token(token)
        except AuthError:
            return False
        return not await REVOKED.is_revoked(principal.token_id)
    api_key = headers.get(b"x-api-key", b"").decode("latin-1")
    configured_key = service_api_key()
    return bool(api_key and configured_key and constant_time_equals(api_key, configured_key))


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
        client = scope.get("client")
        client_ip = client[0] if client else "127.0.0.1"
        hsts = get_settings().environment == "prod"
        method = str(scope.get("method", ""))
        exempt = _throttle_exempt(path)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                if message["status"] == 401 and not exempt and _is_guess(headers, method, path):
                    # credential guessing counts against the client address
                    record_auth_failure(client_ip)
                raw = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw}
                for key, value in _SECURITY_HEADERS.items():
                    if key not in present:
                        raw.append((key, value))
                raw.append((b"content-security-policy", csp.encode()))
                if hsts and b"strict-transport-security" not in present:
                    raw.append((b"strict-transport-security", _HSTS))
                raw.append((b"x-request-id", request_id.encode()))
                message["headers"] = raw
            await send(message)

        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            if (
                not exempt
                and auth_failures_exceeded(client_ip)
                and not await _has_valid_credentials(headers)
            ):
                blocked = JSONResponse(
                    status_code=429,
                    content={"detail": "Çok fazla başarısız kimlik doğrulama — hız sınırı aşıldı"},
                    headers={"Retry-After": "60"},
                )
                await blocked(scope, receive, send_wrapper)
                return
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
