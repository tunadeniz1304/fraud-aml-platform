"""Login / identity endpoints (JWT)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.schemas import LoginIn, TokenOut
from app.security.auth import AuthError, Principal, issue_token
from app.security.deps import current_principal
from app.security.ratelimit import limiter, login_limit

logger = logging.getLogger("fraud.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
@limiter.limit(login_limit)
async def login(request: Request, body: LoginIn) -> TokenOut:
    users = request.app.state.users
    try:
        principal = users.authenticate(body.username, body.password)
    except AuthError:
        logger.warning("[Auth] başarısız giriş denemesi: %s", body.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kullanıcı adı veya parola hatalı",
        ) from None
    token, ttl = issue_token(principal)
    logger.info("[Auth] giriş: %s (%s)", principal.username, principal.role)
    return TokenOut(
        access_token=token,
        expires_in=ttl,
        role=principal.role,
        display_name=principal.display_name,
    )


@router.get("/me")
async def me(principal: Principal = Depends(current_principal)) -> dict[str, str]:
    return {
        "username": principal.username,
        "role": principal.role,
        "display_name": principal.display_name,
        "via": principal.via,
    }
