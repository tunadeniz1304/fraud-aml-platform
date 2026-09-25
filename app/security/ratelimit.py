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
    # "memory://" counts per process; with uvicorn --workers N point it at Redis
    # (RATE_LIMIT_STORAGE_URI=redis://...) so the limit is global, not N x limit
    storage_uri=get_settings().rate_limit_storage_uri,
)


def login_limit() -> str:
    return get_settings().rate_limit_login


def ingest_limit() -> str:
    return get_settings().rate_limit_ingest


def ingest_rate_key(request: Request) -> str:
    """Rate-limit bucket per client: service API key, bearer token, else IP.
    Only a hash prefix is used — the credential itself never becomes a key."""
    import hashlib

    credential = request.headers.get("x-api-key") or request.headers.get("authorization") or ""
    if credential:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
        return f"cred:{digest}"
    return f"ip:{get_remote_address(request)}"


async def rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = exc.detail if isinstance(exc, RateLimitExceeded) else "limit"
    return JSONResponse(
        status_code=429,
        content={"detail": f"Çok fazla istek — hız sınırı aşıldı ({detail})"},
        headers={"Retry-After": "60"},
    )
