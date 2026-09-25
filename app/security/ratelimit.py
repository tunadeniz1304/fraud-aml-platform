"""Rate limiting (slowapi) — configurable per route class via settings."""

from __future__ import annotations

import contextlib

from fastapi import Request
from fastapi.responses import JSONResponse
from limits import RateLimitItem, parse
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


def step_up_limit() -> str:
    return get_settings().rate_limit_step_up


def copilot_limit() -> str:
    return get_settings().rate_limit_copilot


def principal_rate_key(request: Request) -> str:
    """Rate-limit bucket: the authenticated principal, else the client IP.

    The decorator limit is checked after FastAPI resolved the auth
    dependencies, which put the principal on ``request.state``. Headers alone
    never choose the bucket — a client rotating random ``X-API-Key`` or
    ``Authorization`` values stays in its IP bucket until it authenticates.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"{principal.via}:{principal.username}"
    return f"ip:{get_remote_address(request)}"


#: backwards-compatible name (ingest route)
ingest_rate_key = principal_rate_key


def auth_failure_limit() -> RateLimitItem:
    return parse(get_settings().rate_limit_auth_failures)


def auth_failures_exceeded(ip: str) -> bool:
    """True when ``ip`` already produced too many 401 answers in the window."""
    try:
        return not limiter.limiter.test(auth_failure_limit(), "auth-fail", ip)
    except Exception:  # noqa: BLE001 - a broken limiter backend must not block traffic
        return False


def record_auth_failure(ip: str) -> None:
    with contextlib.suppress(Exception):  # counting must never fail the request
        limiter.limiter.hit(auth_failure_limit(), "auth-fail", ip)


async def rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = exc.detail if isinstance(exc, RateLimitExceeded) else "limit"
    return JSONResponse(
        status_code=429,
        content={"detail": f"Çok fazla istek — hız sınırı aşıldı ({detail})"},
        headers={"Retry-After": "60"},
    )
