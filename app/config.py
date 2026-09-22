"""Global configuration and risk thresholds.

Runtime secrets (LLM provider, API keys, model names) are read from the
``.env`` file via pydantic-settings so the system works out of the box with
``LLM_PROVIDER=none`` (rule engine + RAG only, no API key) and upgrades to a
real OpenAI/Anthropic provider when keys are configured.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root (app/config.py -> project root).
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime settings loaded from environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM analysis provider: none | openai | anthropic
    llm_provider: str = "none"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-haiku-latest"

    # --- Risk policy -------------------------------------------------------
    # Any transaction whose risk score is >= RISK_THRESHOLD is autonomously kept
    # blocked; scores below but above WARNING_THRESHOLD are flagged for review.
    risk_threshold: float = 0.75
    warning_threshold: float = 0.50

    # Artificial upper bound a single transaction may add toward amount factor.
    max_amount_factor: float = 1.0

    # How many events per hour count as "high velocity" (fraud-like burst).
    high_velocity_per_hour: int = 10

    # Relative weights of risk signals (must sum to 1.0).
    weight_amount: float = 0.35
    weight_device: float = 0.25
    weight_location: float = 0.20
    weight_time: float = 0.10
    weight_velocity: float = 0.10
    weight_semantic: float = 0.00

    # Semantic (vector) anomaly contribution is added on top of the weighted
    # factors when vector storage is enabled; capped at this share of the score.
    semantic_cap: float = 0.30


settings = Settings()

# --- Backwards-compatible module-level constants ---------------------------
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
VECTOR_DIR = DATA_DIR / "chromadb"
DB_PATH = DATA_DIR / "fraud_agent.db"

RISK_THRESHOLD = settings.risk_threshold
WARNING_THRESHOLD = settings.warning_threshold
MAX_AMOUNT_FACTOR = settings.max_amount_factor
HIGH_VELOCITY_PER_HOUR = settings.high_velocity_per_hour
WEIGHT_AMOUNT = settings.weight_amount
WEIGHT_DEVICE = settings.weight_device
WEIGHT_LOCATION = settings.weight_location
WEIGHT_TIME = settings.weight_time
WEIGHT_VELOCITY = settings.weight_velocity
