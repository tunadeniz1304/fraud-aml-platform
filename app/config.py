"""Global configuration: thresholds, weights, storage, security.

All tunables live here (or in the database / rule files) — the code carries no
magic numbers. Values come from environment variables; ``.env`` files are
loaded **only** through ``python-dotenv`` at runtime (``Anil3/.env`` first,
then the parent ``INGHACK/.env``), never read or logged by the application.
Process environment always wins over ``.env`` (``override=False``).

LLM-specific settings live in :mod:`app.llm.config` (``LLMSettings``).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root (app/config.py -> project root).
BASE_DIR = Path(__file__).resolve().parent.parent

#: ``.env`` search order — the first file that defines a variable wins.
ENV_FILES: tuple[Path, ...] = (BASE_DIR / ".env", BASE_DIR.parent / ".env")


def load_env_files() -> list[Path]:
    """Load ``.env`` files into ``os.environ`` without overriding real env vars.

    Skipped entirely when ``FRAUD_SKIP_DOTENV=1`` (the test-suite sets this so
    no developer key can ever leak into a test run). Returns the files loaded.
    """
    if os.environ.get("FRAUD_SKIP_DOTENV") == "1":
        return []
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a hard dependency
        return []
    loaded: list[Path] = []
    for path in ENV_FILES:
        if path.is_file():
            load_dotenv(path, override=False)
            loaded.append(path)
    return loaded


load_env_files()


def _default_fx() -> dict[str, float]:
    # Kurgusal, sabit demo kurları (1 birim = X TRY). Üretimde FX_RATES_TRY
    # ortam değişkeni (JSON) veya kur servisi ile güncellenir.
    return {"TRY": 1.0, "EUR": 45.0, "USD": 40.0, "GBP": 52.0, "CHF": 47.0, "AED": 10.9}


class Settings(BaseSettings):
    """Runtime settings loaded from the process environment."""

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    # --- Runtime -----------------------------------------------------------
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False
    api_docs: bool = Field(default=False, description="Swagger UI (/docs) açık mı")

    # --- Storage -----------------------------------------------------------
    data_dir: Path = BASE_DIR / "data"
    db_path: Path | None = Field(default=None, validation_alias="FRAUD_DB_PATH")
    customers_path: Path | None = Field(default=None, validation_alias="FRAUD_CUSTOMERS_PATH")
    transactions_path: Path | None = Field(default=None, validation_alias="FRAUD_TRANSACTIONS_PATH")

    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    db_auto_create: bool = True
    writer_batch_size: int = 500
    writer_flush_ms: int = 50
    writer_queue_size: int = 20_000

    # --- Event bus (P0.2) --------------------------------------------------
    event_bus: Literal["memory", "redis"] = "memory"
    redis_url: str | None = Field(default=None, validation_alias="REDIS_URL")
    redis_stream_prefix: str = "fraud"
    bus_max_retries: int = 2
    bus_partitions: int = 0
    bus_queue_size: int = 1_000
    bus_claim_idle_ms: int = 5_000

    # --- Feature store (P0.4) ----------------------------------------------
    feature_store: Literal["auto", "memory", "redis"] = "auto"
    feature_max_entities: int = 200_000
    profile_alpha: float = 0.05
    profile_prior_weight: float = 5.0
    night_start_hour: int = 0
    night_end_hour: int = 6
    unusual_hour_prob: float = 0.02
    structuring_threshold_try: float = 50_000.0
    structuring_band: float = 0.90
    large_transfer_factor: float = 1.5

    # --- Stream / simulation -----------------------------------------------
    stream_mode: Literal["batch", "stream", "off"] = "batch"
    stream_rate: float = 2.0
    stream_drift: float = 0.0

    # --- Vector store (copilot RAG; never on the synchronous scoring path) ---
    vector_store: Literal["chroma", "off"] = "chroma"
    vector_embedding: Literal["onnx", "hash"] = "onnx"
    vector_dir: Path | None = Field(default=None, validation_alias="VECTOR_DIR")

    # --- Legacy two-level policy (kept for the v1 agents) -------------------
    risk_threshold: float = 0.75
    warning_threshold: float = 0.50
    high_velocity_per_hour: int = 10
    unknown_customer_risk: float = 0.60

    # Relative weights of the v1 rule-score signals (sum to 1.0).
    weight_amount: float = 0.32
    weight_device: float = 0.22
    weight_location: float = 0.16
    weight_time: float = 0.08
    weight_velocity: float = 0.06
    weight_country: float = 0.07
    weight_purpose: float = 0.05
    weight_ip: float = 0.02
    weight_channel: float = 0.02

    # Amount ratio (x customer average) at which the amount signal saturates.
    amount_saturation_ratio: float = 10.0
    # Score contribution of an off-hours transfer (0..1).
    off_hours_score: float = 0.7

    # --- Hybrid scoring / policy (P0.5) --------------------------------------
    rules_path: Path | None = Field(default=None, validation_alias="RULES_PATH")
    models_dir: Path | None = Field(default=None, validation_alias="MODELS_DIR")
    model_enabled: bool = True
    policy_step_up: float = 0.35
    policy_hold: float = 0.60
    policy_block: float = 0.85
    # Weights of external signals in risk = 1-(1-stack)·Π(1-w·s).
    policy_signal_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "graph": 0.60,
            "burst": 0.25,
            "app": 0.55,
            "online": 0.15,
            "consortium": 0.60,
        }
    )
    hold_cooling_off_minutes: int = 30
    reason_top_k: int = 5
    # TreeSHAP (~2 ms) runs for non-ALLOW decisions and for risk >= this value.
    explain_min_risk: float = 0.2
    sanctions_fuzzy_threshold: float = 0.93

    # --- Entity graph / APP scam / online profile (P1.1–P1.3) ---------------
    payees_path: Path | None = Field(default=None, validation_alias="FRAUD_PAYEES_PATH")
    graph_enabled: bool = True
    ring_detect_interval_s: int = 120
    batch_rebase_ts: bool = False
    app_high_amount_ratio: float = 3.0
    app_long_session_s: float = 900.0
    app_young_payee_days: int = 90
    app_confirm_threshold: float = 0.5
    app_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "new_payee_high": 0.30,
            "first_large": 0.15,
            "purpose": 0.45,
            "active_call": 0.45,
            "remote_access": 0.50,
            "long_session": 0.15,
            "vulnerable": 0.20,
            "cop_no_match": 0.45,
            "cop_close": 0.10,
            "young_payee": 0.20,
            "mule_payee": 0.50,
            "known_payee_damping": 0.3,
        }
    )
    online_anomaly_enabled: bool = True
    consortium_enabled: bool = True
    # Konsorsiyum tuzu (üyeler arası paylaşılan gizli değer; demo varsayılanı).
    consortium_salt: str = "anil3-consortium-demo"
    frontend_dist: Path | None = Field(default=None, validation_alias="FRONTEND_DIST")
    fp_cost_try: float = 50.0  # operasyonel maliyet: bir yanlış alarmın inceleme maliyeti
    retrain_min_new_labels: int = 50
    #: scored-but-unlearned events kept for late step-up / analyst feedback
    feedback_pending_max: int = 50_000
    auto_sib_case_types: list[str] = Field(default_factory=lambda: ["AML", "MULE", "YAPTIRIM"])
    online_anomaly_reason_min: float = 0.9

    # --- Case management (P0.6) ----------------------------------------------
    case_group_window_hours: int = 24
    internal_sla_hours: int = 4
    masak_business_days: int = 10
    masak_warning_business_days: int = 2
    evidence_max_bytes: int = 1_000_000
    # Resmi tatiller (MASAK iş günü hesabı): 2026 için sabit + dini bayram günleri.
    tr_holidays: list[str] = Field(
        default_factory=lambda: [
            "2026-01-01",
            "2026-03-20",
            "2026-03-21",
            "2026-03-22",
            "2026-04-23",
            "2026-05-01",
            "2026-05-19",
            "2026-05-26",
            "2026-05-27",
            "2026-05-28",
            "2026-05-29",
            "2026-07-15",
            "2026-08-30",
            "2026-10-29",
        ]
    )

    high_risk_countries: str = "NG,AE,RU,UA,KP,IR,SY,CU"
    fx_rates_try: dict[str, float] = Field(default_factory=_default_fx)

    # --- Memory bounds (bug #11) --------------------------------------------
    analyzed_buffer: int = 5000
    recent_window_seconds: int = 3600
    max_tracked_customers: int = 50_000

    # --- Security ----------------------------------------------------------
    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_minutes: int = 480
    admin_token: SecretStr = SecretStr("")
    service_api_key: SecretStr = SecretStr("")
    service_hmac_secret: SecretStr = SecretStr("")
    hmac_max_skew_seconds: int = 300
    demo_pw_analist: SecretStr = SecretStr("analist123")
    demo_pw_kidemli: SecretStr = SecretStr("kidemli123")
    demo_pw_admin: SecretStr = SecretStr("admin123")
    cors_origins: list[str] = Field(default_factory=list)
    rate_limit_default: str = "1200/minute"
    rate_limit_login: str = "10/minute"
    rate_limit_ingest: str = "200000/minute"

    # --- Convenience -------------------------------------------------------
    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or self.data_dir / "fraud_platform.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.resolved_db_path.as_posix()}"

    @property
    def resolved_feature_store(self) -> str:
        if self.feature_store == "auto":
            return "redis" if self.redis_url else "memory"
        return self.feature_store

    @property
    def resolved_vector_dir(self) -> Path:
        return self.vector_dir or self.data_dir / "chromadb"

    @property
    def resolved_rules_path(self) -> Path:
        return self.rules_path or BASE_DIR / "rules" / "core.yaml"

    @property
    def resolved_models_dir(self) -> Path:
        return self.models_dir or BASE_DIR / "models"

    @property
    def resolved_frontend_dist(self) -> Path:
        return self.frontend_dist or BASE_DIR / "frontend" / "dist"

    @property
    def resolved_payees_path(self) -> Path:
        return self.payees_path or self.data_dir / "payees.json"

    @property
    def resolved_customers_path(self) -> Path:
        return self.customers_path or self.data_dir / "customers.json"

    @property
    def resolved_transactions_path(self) -> Path:
        return self.transactions_path or self.data_dir / "transactions.json"

    @property
    def high_risk_country_set(self) -> frozenset[str]:
        return frozenset(
            c.strip().upper() for c in self.high_risk_countries.split(",") if c.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (tests may ``get_settings.cache_clear()``)."""
    return Settings()


settings = get_settings()

# --- Backwards-compatible module-level constants ---------------------------
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
VECTOR_DIR = DATA_DIR / "chromadb"
DB_PATH = DATA_DIR / "fraud_agent.db"

RISK_THRESHOLD = settings.risk_threshold
WARNING_THRESHOLD = settings.warning_threshold
HIGH_VELOCITY_PER_HOUR = settings.high_velocity_per_hour
