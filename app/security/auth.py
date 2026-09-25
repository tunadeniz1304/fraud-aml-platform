"""Authentication & RBAC (P0.8, fixes bug #10).

* **JWT (HS256)** for analysts: ``POST /api/auth/login`` issues a token for the
  seeded demo users ``analist`` / ``kidemli_analist`` / ``admin``.
* **Roles are hierarchical**: ``analist < kidemli_analist < admin``.
* **Break-glass admin token** (legacy ``ADMIN_TOKEN``) compared in constant
  time with :func:`hmac.compare_digest`.
* **Service credentials** for ``POST /api/transactions``: ``X-API-Key``
  (constant-time) or an HMAC-SHA256 body signature
  (``X-Timestamp`` + ``X-Signature: sha256=<hex>``, replay window bounded).
* Passwords are stored as PBKDF2-HMAC-SHA256 hashes, never in clear.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import jwt

from app.config import get_settings

logger = logging.getLogger("fraud.security")

Role = Literal["analist", "kidemli_analist", "admin", "service"]
ROLE_RANK: dict[str, int] = {"service": 0, "analist": 1, "kidemli_analist": 2, "admin": 3}
ROLE_LABELS = {
    "analist": "Analist",
    "kidemli_analist": "Kıdemli Analist",
    "admin": "Yönetici",
    "service": "Servis",
}
#: audit identity of the static ADMIN_TOKEN — the ``:`` keeps it out of the
#: user namespace, so it can never equal (or impersonate) a directory user
BREAK_GLASS_USERNAME = "break-glass:admin-token"
_PBKDF2_ROUNDS = 120_000
_ALGORITHM = "HS256"


class AuthError(Exception):
    """Authentication failed (→ 401)."""


class ForbiddenError(Exception):
    """Authenticated but not allowed (→ 403)."""


@dataclass(frozen=True)
class Principal:
    username: str
    role: Role
    display_name: str = ""
    via: str = "jwt"
    # JWT id and expiry (unix s) — used by logout/revocation; not part of identity
    token_id: str = field(default="", compare=False)
    expires_at: int = field(default=0, compare=False)

    def has_role(self, minimum: Role) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[minimum]


# --- password hashing --------------------------------------------------------
def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt_b64}${digest_b64}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt_b64, digest_b64 = encoded.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    return hmac.compare_digest(actual, expected)


# --- user directory ------------------------------------------------------------
@dataclass
class UserRecord:
    username: str
    role: Role
    display_name: str
    password_hash: str


@dataclass
class UserDirectory:
    """In-memory user store (DB-backed from F2; same interface)."""

    users: dict[str, UserRecord] = field(default_factory=dict)

    @classmethod
    def with_demo_users(cls) -> UserDirectory:
        s = get_settings()
        directory = cls()
        for username, role, name, pw in (
            ("analist", "analist", "Demo Analist", s.demo_pw_analist),
            ("kidemli_analist", "kidemli_analist", "Demo Kıdemli Analist", s.demo_pw_kidemli),
            ("admin", "admin", "Demo Yönetici", s.demo_pw_admin),
        ):
            directory.add(username, role, name, pw.get_secret_value())  # type: ignore[arg-type]
        return directory

    @classmethod
    def from_settings(cls, settings: Any = None) -> UserDirectory:
        """Demo users only when ``seed_demo_users``; never in prod."""
        s = settings or get_settings()
        if not s.seed_demo_users:
            return cls()
        if s.environment == "prod":
            raise RuntimeError(
                "Prod ortamında demo kullanıcılar (varsayılan parolalı) açılamaz — "
                "SEED_DEMO_USERS=false yapın"
            )
        return cls.with_demo_users()

    def add(self, username: str, role: Role, display_name: str, password: str) -> None:
        if not username or ":" in username:
            raise ValueError("Kullanıcı adı boş olamaz ve ':' içeremez")
        self.users[username] = UserRecord(username, role, display_name, hash_password(password))

    def authenticate(self, username: str, password: str) -> Principal:
        record = self.users.get(username)
        # Always run the hash to keep timing independent of user existence.
        encoded = record.password_hash if record else hash_password("x", salt=b"0" * 16)
        ok = verify_password(password, encoded)
        if record is None or not ok:
            raise AuthError("Kullanıcı adı veya parola hatalı")
        return Principal(record.username, record.role, record.display_name)


# --- JWT -------------------------------------------------------------------------
_EPHEMERAL_SECRET: str | None = None


def _jwt_secret() -> str:
    global _EPHEMERAL_SECRET
    configured = get_settings().jwt_secret.get_secret_value()
    if configured:
        return configured
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
        logger.warning(
            "[Auth] JWT_SECRET tanımlı değil — süreç ömürlü geçici anahtar üretildi "
            "(yeniden başlatınca oturumlar düşer)"
        )
    return _EPHEMERAL_SECRET


#: ``iss`` / ``aud`` claims: a token minted for another service that happens
#: to share the signing secret is not accepted by this API
JWT_ISSUER = "anil3-fraud"
JWT_AUDIENCE = "anil3-api"


def issue_token(principal: Principal, *, ttl_minutes: int | None = None) -> tuple[str, int]:
    ttl = (ttl_minutes or get_settings().jwt_ttl_minutes) * 60
    now = int(time.time())
    payload = {
        "sub": principal.username,
        "role": principal.role,
        "name": principal.display_name,
        "iat": now,
        "exp": now + ttl,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_ALGORITHM), ttl


def decode_token(token: str) -> Principal:
    try:
        data = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Oturum süresi doldu") from None
    except jwt.PyJWTError:
        raise AuthError("Geçersiz token") from None
    role = data.get("role")
    if role not in ROLE_RANK:
        raise AuthError("Geçersiz rol")
    if not data.get("jti"):
        raise AuthError("Geçersiz token")
    return Principal(
        str(data["sub"]),
        role,
        str(data.get("name", "")),
        token_id=str(data["jti"]),
        expires_at=int(data.get("exp", 0)),
    )


def signed_by_us(token: str) -> bool:
    """True when ``token`` carries this API's signature, issuer and audience,
    even if it has expired. Such a token cannot be a guess (only the server
    can mint it); an expired or revoked session is not credential stuffing."""
    try:
        jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        return False
    return True


# --- static credentials ----------------------------------------------------------
def _configured(env_name: str, fallback: str) -> str:
    # Read at call time so rotations (and tests) take effect without restart.
    return os.environ.get(env_name, "").strip() or fallback


def admin_token() -> str:
    return _configured("ADMIN_TOKEN", get_settings().admin_token.get_secret_value())


def service_api_key() -> str:
    return _configured("SERVICE_API_KEY", get_settings().service_api_key.get_secret_value())


def service_hmac_secret() -> str:
    return _configured("SERVICE_HMAC_SECRET", get_settings().service_hmac_secret.get_secret_value())


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def sign_body(secret: str, timestamp: str, body: bytes, nonce: str = "") -> str:
    """``sha256=hex(HMAC(secret, timestamp "." nonce "." body))``."""
    message = timestamp.encode("utf-8") + b"." + nonce.encode("utf-8") + b"." + body
    mac = hmac.new(secret.encode("utf-8"), message, "sha256")
    return "sha256=" + mac.hexdigest()


def verify_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    *,
    nonce: str | None = None,
    now: float | None = None,
) -> bool:
    """Signature + timestamp window check. The nonce is part of the signed
    message; single use is enforced by :class:`NonceStore` (``X-Nonce``)."""
    if not secret or not timestamp or not signature or not nonce:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    skew = abs((now or time.time()) - ts)
    if skew > get_settings().hmac_max_skew_seconds:
        return False
    expected = sign_body(secret, timestamp, body, nonce)
    return constant_time_equals(expected, signature.strip())


class NonceStore:
    """Replay protection: a nonce is accepted once within the timestamp window.

    Redis (``SET NX EX``) when available so every worker shares it, otherwise
    a bounded in-process map.
    """

    def __init__(self, redis: Any = None, *, max_entries: int = 100_000) -> None:
        self.redis = redis
        self._seen: dict[str, float] = {}
        self.max_entries = max_entries

    async def claim(self, nonce: str) -> bool:
        window = 2 * get_settings().hmac_max_skew_seconds
        if self.redis is not None:
            return bool(await self.redis.set(f"hmac:nonce:{nonce}", "1", nx=True, ex=window))
        now = time.time()
        if len(self._seen) >= self.max_entries:
            self._seen = {n: t for n, t in self._seen.items() if t > now}
        if self._seen.get(nonce, 0.0) > now:
            return False
        self._seen[nonce] = now + window
        return True
