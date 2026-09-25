"""LLM configuration contract (§3.2).

The **only** place the code reads LLM environment variables. Aliases are tried
in order and the first *non-empty* one wins:

* key:      ``LLM_API_KEY`` → ``DEEPSEEK_API_KEY`` → ``EVREN_API_KEY`` → ``OPENAI_API_KEY``
* base URL: ``LLM_BASE_URL`` → ``DEEPSEEK_BASE_URL`` → ``EVREN_BASE_URL`` → ``OPENAI_BASE_URL``
* model:    ``LLM_MODEL`` → ``DEEPSEEK_MODEL``

``.env`` files are loaded by :func:`app.config.load_env_files` (python-dotenv,
``override=False``); this module never opens them. The API key is a
``SecretStr`` so it can never appear in a repr, log line or HTTP response.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

import app.config  # noqa: F401  - ensures .env files are loaded first

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
KEY_ENV_NAMES = ("LLM_API_KEY", "DEEPSEEK_API_KEY", "EVREN_API_KEY", "OPENAI_API_KEY")


class LLMConfigError(RuntimeError):
    """Raised at start-up when the LLM configuration is unusable."""


def mask_secret(value: str | SecretStr | None) -> str:
    """Mask a credential as ``sk-…abcd`` (never reveals more than 3+4 chars)."""
    raw = value.get_secret_value() if isinstance(value, SecretStr) else (value or "")
    if not raw:
        return ""
    if len(raw) < 12:
        return "***"
    return f"{raw[:3]}…{raw[-4:]}"


class LLMSettings(BaseSettings):
    """Validated LLM settings (pydantic-settings, env only)."""

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True, populate_by_name=True)

    provider: Literal["openai", "anthropic"] = Field(
        default="openai", validation_alias=AliasChoices("LLM_PROVIDER")
    )
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices(*KEY_ENV_NAMES))
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        validation_alias=AliasChoices(
            "LLM_BASE_URL", "DEEPSEEK_BASE_URL", "EVREN_BASE_URL", "OPENAI_BASE_URL"
        ),
    )
    model: str = Field(
        default=DEFAULT_MODEL, validation_alias=AliasChoices("LLM_MODEL", "DEEPSEEK_MODEL")
    )
    mode: Literal["auto", "live", "demo"] = Field(
        default="auto", validation_alias=AliasChoices("LLM_MODE")
    )
    timeout_seconds: float = Field(
        default=20.0, gt=0, validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS")
    )
    max_retries: int = Field(default=2, ge=0, validation_alias=AliasChoices("LLM_MAX_RETRIES"))
    temperature: float = Field(
        default=0.2, ge=0, le=2, validation_alias=AliasChoices("LLM_TEMPERATURE")
    )
    max_tokens: int = Field(default=1200, gt=0, validation_alias=AliasChoices("LLM_MAX_TOKENS"))

    # Optional Anthropic path (LLM_PROVIDER=anthropic).
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )
    anthropic_model: str = Field(
        default="claude-haiku-4-5", validation_alias=AliasChoices("ANTHROPIC_MODEL")
    )

    # --- derived -------------------------------------------------------------
    @property
    def effective_key(self) -> SecretStr:
        return self.anthropic_api_key if self.provider == "anthropic" else self.api_key

    @property
    def effective_model(self) -> str:
        return self.anthropic_model if self.provider == "anthropic" else self.model

    @property
    def key_present(self) -> bool:
        return bool(self.effective_key.get_secret_value().strip())

    @property
    def base_url_host(self) -> str:
        if self.provider == "anthropic":
            return "api.anthropic.com"
        return urlparse(self.base_url).hostname or ""

    def resolve_mode(self) -> Literal["live", "demo"]:
        """Apply §3.3: ``auto`` → live iff a key exists; ``live`` needs a key."""
        if self.mode == "demo":
            return "demo"
        if self.mode == "live":
            if not self.key_present:
                raise LLMConfigError(
                    "LLM_MODE=live ancak API anahtarı bulunamadı "
                    f"({' / '.join(KEY_ENV_NAMES)} tanımlı değil). "
                    "Anahtarı .env dosyasına ekleyin veya LLM_MODE=auto/demo kullanın."
                )
            return "live"
        return "live" if self.key_present else "demo"

    def startup_line(self) -> str:
        """Single start-up log line (never includes the key)."""
        try:
            mode = self.resolve_mode()
        except LLMConfigError:
            return "LLM: HATA (LLM_MODE=live ama anahtar yok)"
        if mode == "live":
            return f"LLM: CANLI ({self.effective_model} @ {self.base_url_host})"
        reason = "LLM_MODE=demo" if self.mode == "demo" else "anahtar bulunamadı"
        return f"LLM: DEMO modu ({reason})"
