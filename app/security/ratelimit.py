"""Rate limiting (slowapi) — configurable per route class via settings."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import get_settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[get_settings().rate_limit_default],
    headers_enabled=False,
)


def login_limit() -> str:
    return get_settings().rate_limit_login


def ingest_limit() -> str:
    return get_settings().rate_limit_ingest


async def rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = exc.detail if isinstance(exc, RateLimitExceeded) else "limit"
    return JSONResponse(
        status_code=429,
        content={"detail": f"Çok fazla istek — hız sınırı aşıldı ({detail})"},
        headers={"Retry-After": "60"},
    )
