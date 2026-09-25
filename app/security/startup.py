"""Fail-fast configuration checks run before the API or the worker starts.

A production process must not come up with demo secrets: an empty, short or
placeholder ``JWT_SECRET``, the seeded demo users (known passwords), the
public demo consortium salt, a missing audit-chain HMAC key, a weak
break-glass ``ADMIN_TOKEN`` / ``SERVICE_API_KEY`` or per-process (``memory://``)
rate-limit counters. Independently of the environment, several uvicorn
workers need a shared ``JWT_SECRET`` (otherwise each worker mints its own
ephemeral signing key) and Redis (SSE tickets, HMAC nonces, account-status
propagation and rate limits are otherwise per process).

Both the API (:func:`app.api.dashboard.create_app`) and the background
worker (:func:`app.worker.main`) call :func:`enforce_startup_policy`.
"""

from __future__ import annotations

from typing import Any

#: minimum length of a production JWT signing secret (HS256: >= 256 bit)
MIN_JWT_SECRET_LENGTH = 32
#: minimum length of a production static credential (ADMIN_TOKEN, SERVICE_API_KEY,
#: SERVICE_HMAC_SECRET) — a bearer secret compared as-is, so >= 256 bit as well
MIN_STATIC_CREDENTIAL_LENGTH = 32
#: the demo consortium salt shipped in ``app.config`` (public, in the repo)
DEMO_CONSORTIUM_SALT = "anil3-consortium-demo"
_PLACEHOLDER_SECRETS = frozenset(
    {"changeme", "change-me", "change_me", "secret", "jwt-secret", "please-change", "default"}
)
#: values that appear in the repo, the docs or the test-suite — never a real secret
_DEMO_CREDENTIALS = frozenset(
    {"admin", "admin123", "analist123", "kidemli123", "s3cret", "password", "demo", "test"}
)
_DEMO_MARKERS = ("change", "demo", "example", "placeholder")


def _weak_secret(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        lowered in _PLACEHOLDER_SECRETS
        or lowered in _DEMO_CREDENTIALS
        or any(marker in lowered for marker in _DEMO_MARKERS)
    )


def _static_credential_problems(settings: Any) -> list[str]:
    problems: list[str] = []
    for env_name, field in (
        ("ADMIN_TOKEN", "admin_token"),
        ("SERVICE_API_KEY", "service_api_key"),
        ("SERVICE_HMAC_SECRET", "service_hmac_secret"),
    ):
        secret = getattr(settings, field, None)
        value = secret.get_secret_value() if secret is not None else ""
        if not value:
            continue  # unset: the credential is simply disabled
        if _weak_secret(value):
            problems.append(f"{env_name} örnek/demo bir değer — rastgele bir anahtar üretin")
        elif len(value) < MIN_STATIC_CREDENTIAL_LENGTH:
            problems.append(
                f"{env_name} tanımlıysa en az {MIN_STATIC_CREDENTIAL_LENGTH} karakter olmalıdır"
            )
    return problems


def startup_problems(settings: Any) -> list[str]:
    """Human-readable (Turkish) reasons why this configuration must not start."""
    problems: list[str] = []
    secret = settings.jwt_secret.get_secret_value()
    several_workers = int(settings.web_concurrency) > 1
    if several_workers and not secret:
        problems.append(
            "WEB_CONCURRENCY > 1 iken JWT_SECRET zorunludur — her worker ayrı geçici "
            "anahtar üretir ve diğer worker'ların token'larını reddeder"
        )
    if several_workers and not settings.redis_url:
        problems.append(
            "WEB_CONCURRENCY > 1 iken REDIS_URL zorunludur — SSE biletleri, HMAC nonce'ları, "
            "hesap durumu yayılımı ve hız sınırları aksi halde süreç başına tutulur"
        )
    if settings.environment != "prod":
        return problems
    if not secret:
        problems.append("Prod ortamında JWT_SECRET tanımlı olmalıdır")
    elif secret.strip().lower() in _PLACEHOLDER_SECRETS or "change" in secret.lower():
        problems.append("JWT_SECRET örnek/varsayılan bir değer — rastgele bir anahtar üretin")
    elif len(secret) < MIN_JWT_SECRET_LENGTH:
        problems.append(f"JWT_SECRET en az {MIN_JWT_SECRET_LENGTH} karakter olmalıdır")
    if settings.seed_demo_users:
        problems.append(
            "Prod ortamında demo kullanıcılar (bilinen parolalar) açılamaz — "
            "SEED_DEMO_USERS=false yapın"
        )
    if settings.consortium_enabled and settings.consortium_salt == DEMO_CONSORTIUM_SALT:
        problems.append(
            "Prod ortamında demo konsorsiyum tuzu kullanılamaz — CONSORTIUM_SALT tanımlayın "
            "ya da CONSORTIUM_ENABLED=false yapın"
        )
    audit_key = settings.audit_hmac_key.get_secret_value()
    if not audit_key:
        problems.append(
            "Prod ortamında AUDIT_HMAC_KEY zorunludur — anahtarsız denetim zinciri, "
            "veritabanına yazabilen herkes tarafından yeniden hesaplanabilir"
        )
    elif _weak_secret(audit_key) or len(audit_key) < MIN_JWT_SECRET_LENGTH:
        problems.append(
            f"AUDIT_HMAC_KEY rastgele ve en az {MIN_JWT_SECRET_LENGTH} karakter olmalıdır"
        )
    problems.extend(_static_credential_problems(settings))
    if str(settings.rate_limit_storage_uri).strip().lower().startswith("memory://"):
        problems.append(
            "Prod ortamında RATE_LIMIT_STORAGE_URI=memory:// kullanılamaz — sayaçlar süreç "
            "başına tutulur ve yeniden başlatmada sıfırlanır; ör. redis://redis:6379/1"
        )
    return problems


def enforce_startup_policy(settings: Any) -> None:
    """Raise ``RuntimeError`` listing every problem; start-up aborts."""
    problems = startup_problems(settings)
    if problems:
        raise RuntimeError("Güvensiz yapılandırma, başlatma durduruldu: " + "; ".join(problems))
