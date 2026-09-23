"""API hardening middleware for the dashboard.

- Optional ``ADMIN_TOKEN`` from env: when set, mutating admin endpoints require
  ``Authorization: Bearer <token>`` (all other endpoints stay open for the
  live dashboard).
- Security headers on every response (CSP, X-Content-Type-Options, etc.).
- Request logging with latency.
"""

from __future__ import annotations

import logging
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("fraud.security")

# Mutating paths that require ADMIN_TOKEN when configured.
_MUTATING_PREFIXES = ("/api/admin",)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-XSS-Protection": "1; mode=block",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'"
    ),
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers and access logging to every response."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response: Response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        logger.info(
            "%s %s -> %s (%d ms)",
            request.method, request.url.path, response.status_code, round(elapsed_ms, 1),
        )
        return response


class AdminTokenMiddleware(BaseHTTPMiddleware):
    """Enforce ``ADMIN_TOKEN`` on mutating endpoints when configured."""

    async def dispatch(self, request: Request, call_next):
        admin_token = os.getenv("ADMIN_TOKEN", "").strip()
        if admin_token and request.url.path.startswith(_MUTATING_PREFIXES):
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            if token != admin_token:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Yetkisiz: geçerli ADMIN_TOKEN gerekli"},
                )
        return await call_next(request)


def install(app) -> None:  # noqa: ANN001 - Starlette/FastAPI app
    """Install hardening middleware in dependency-free ascending order."""
    app.add_middleware(AdminTokenMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
