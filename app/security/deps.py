"""FastAPI dependencies for authentication and role checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.security.auth import (
    AuthError,
    Principal,
    Role,
    admin_token,
    constant_time_equals,
    decode_token,
    service_api_key,
    service_hmac_secret,
    verify_signature,
)

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str = "Kimlik doğrulama gerekli") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def principal_from_token(token: str) -> Principal:
    configured = admin_token()
    if configured and constant_time_equals(token, configured):
        return Principal("admin-token", "admin", "Break-glass yönetici", via="admin_token")
    try:
        return decode_token(token)
    except AuthError as exc:
        raise _unauthorized(str(exc)) from None


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if creds is None or not creds.credentials:
        raise _unauthorized()
    return principal_from_token(creds.credentials)


async def stream_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Like :func:`current_principal`, but also accepts ``?access_token=`` (SSE)."""
    if creds is not None and creds.credentials:
        return principal_from_token(creds.credentials)
    token = request.query_params.get("access_token")
    if not token:
        raise _unauthorized()
    return principal_from_token(token)


def require_role(minimum: Role) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: authenticated principal with at least ``minimum`` role."""

    async def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_role(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Bu işlem için en az '{minimum}' rolü gerekli",
            )
        return principal

    return _dep


async def ingest_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Service key, HMAC signature or an analyst JWT may ingest transactions."""
    api_key = request.headers.get("x-api-key")
    configured_key = service_api_key()
    if api_key:
        if configured_key and constant_time_equals(api_key, configured_key):
            return Principal("ingest-service", "service", "Servis", via="api_key")
        raise _unauthorized("Geçersiz servis anahtarı")
    signature = request.headers.get("x-signature")
    if signature:
        body = await request.body()
        if verify_signature(
            service_hmac_secret(), request.headers.get("x-timestamp"), signature, body
        ):
            return Principal("ingest-service", "service", "Servis", via="hmac")
        raise _unauthorized("Geçersiz HMAC imzası")
    if creds is not None and creds.credentials:
        return principal_from_token(creds.credentials)
    raise _unauthorized("Servis kimliği (X-API-Key / X-Signature) veya oturum gerekli")
