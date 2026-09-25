"""Fail-fast configuration checks run before the API or the worker starts.

A production process must not come up with demo secrets: an empty, short or
placeholder ``JWT_SECRET``, the seeded demo users (known passwords) or the
public demo consortium salt. Independently of the environment, several uvicorn
workers without a shared ``JWT_SECRET`` would each mint their own ephemeral
signing key, so a token issued by one worker is rejected by the others.
"""

from __future__ import annotations

from typing import Any

#: minimum length of a production JWT signing secret (HS256: >= 256 bit)
MIN_JWT_SECRET_LENGTH = 32
#: the demo consortium salt shipped in ``app.config`` (public, in the repo)
DEMO_CONSORTIUM_SALT = "anil3-consortium-demo"
_PLACEHOLDER_SECRETS = frozenset(
    {"changeme", "change-me", "change_me", "secret", "jwt-secret", "please-change", "default"}
)


def startup_problems(settings: Any) -> list[str]:
    """Human-readable (Turkish) reasons why this configuration must not start."""
    problems: list[str] = []
    secret = settings.jwt_secret.get_secret_value()
    if int(settings.web_concurrency) > 1 and not secret:
        problems.append(
            "WEB_CONCURRENCY > 1 iken JWT_SECRET zorunludur — her worker ayrı geçici "
            "anahtar üretir ve diğer worker'ların token'larını reddeder"
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
    return problems


def enforce_startup_policy(settings: Any) -> None:
    """Raise ``RuntimeError`` listing every problem; start-up aborts."""
    problems = startup_problems(settings)
    if problems:
        raise RuntimeError("Güvensiz yapılandırma, başlatma durduruldu: " + "; ".join(problems))
