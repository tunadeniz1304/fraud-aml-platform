"""FastAPI dependencies for authentication and role checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.security.auth import (
    AuthError,
    NonceStore,
    Principal,
    Role,
    admin_token,
    constant_time_equals,
    decode_token,
    service_api_key,
    service_hmac_secret,
    verify_signature,
)
from app.security.revocation import REVOKED
from app.security.tickets import TICKETS

_bearer = HTTPBearer(auto_error=False)


def nonce_store(request: Request) -> NonceStore:
    store = getattr(request.app.state, "nonces", None)
    if store is None:
        from app.api.state import state

        redis = getattr(state.pipeline, "redis", None) if state.pipeline else None
        store = request.app.state.nonces = NonceStore(redis)
    return store


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


async def _session_principal(request: Request, token: str) -> Principal:
    """Bearer token → principal, re-validated against current state.

    A signed JWT alone is not enough: the token must not have been revoked
    (logout), and the user must still exist in the user directory — its role
    is taken from the directory, so a demotion or removal applies to tokens
    already issued, not only after they expire. The break-glass ADMIN_TOKEN
    has no directory entry and is not checked there.
    """
    principal = principal_from_token(token)
    if principal.via != "jwt":
        return principal
    if await REVOKED.is_revoked(principal.token_id):
        raise _unauthorized("Oturum sonlandırıldı — yeniden giriş yapın")
    users = getattr(request.app.state, "users", None)
    if users is None:
        return principal
    record = users.users.get(principal.username)
    if record is None:
        raise _unauthorized("Kullanıcı hesabı bulunamadı ya da devre dışı")
    return replace(principal, role=record.role, display_name=record.display_name)


def _bind(request: Request, principal: Principal) -> Principal:
    """Remember the authenticated principal: the rate-limit key switches from
    the client IP to the principal only after authentication succeeded."""
    request.state.principal = principal
    return principal


async def current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if creds is None or not creds.credentials:
        raise _unauthorized()
    return _bind(request, await _session_principal(request, creds.credentials))


async def stream_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Bearer header, or a single-use SSE ticket (``?ticket=``) from
    ``POST /api/stream/ticket``. JWTs in the query string (``?access_token=``)
    are refused: they end up in access logs and browser history."""
    if creds is not None and creds.credentials:
        return _bind(request, await _session_principal(request, creds.credentials))
    ticket = request.query_params.get("ticket")
    principal = await TICKETS.redeem(ticket) if ticket else None
    if principal is None:
        raise _unauthorized("Geçerli bir SSE bileti gerekli (POST /api/stream/ticket)")
    return _bind(request, principal)


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


async def _service_credentials(request: Request) -> Principal | None:
    """``X-API-Key`` or HMAC-signed body; ``None`` when neither header is sent."""
    api_key = request.headers.get("x-api-key")
    configured_key = service_api_key()
    if api_key:
        if configured_key and constant_time_equals(api_key, configured_key):
            return _bind(request, Principal("ingest-service", "service", "Servis", via="api_key"))
        raise _unauthorized("Geçersiz servis anahtarı")
    signature = request.headers.get("x-signature")
    if signature:
        body = await request.body()
        nonce = request.headers.get("x-nonce")
        if verify_signature(
            service_hmac_secret(), request.headers.get("x-timestamp"), signature, body, nonce=nonce
        ):
            if nonce and await nonce_store(request).claim(nonce):
                return _bind(request, Principal("ingest-service", "service", "Servis", via="hmac"))
            raise _unauthorized("HMAC nonce daha önce kullanıldı (tekrar saldırısı)")
        raise _unauthorized("Geçersiz HMAC imzası")
    return None


async def ingest_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Service key or HMAC signature; an analyst JWT only outside prod
    (demo/test convenience — in prod ingest is a machine-to-machine path)."""
    service = await _service_credentials(request)
    if service is not None:
        return service
    if creds is not None and creds.credentials:
        if get_settings().environment == "prod":
            raise _unauthorized(
                "Prod ortamında işlem girişi yalnızca servis kimliğiyle "
                "(X-API-Key / X-Signature) yapılabilir"
            )
        return _bind(request, await _session_principal(request, creds.credentials))
    raise _unauthorized("Servis kimliği (X-API-Key / X-Signature) veya oturum gerekli")


async def service_principal(request: Request) -> Principal:
    """Service credentials only (API key / HMAC) — never an analyst session.
    Used by channel callbacks such as the step-up (OTP) result."""
    service = await _service_credentials(request)
    if service is None:
        raise _unauthorized("Servis kimliği (X-API-Key / X-Signature) gerekli")
    return service
