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

from pydantic import Field, SecretStr, model_validator
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
    #: uvicorn worker count (same env var uvicorn reads); >1 needs a JWT_SECRET
    web_concurrency: int = 1

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
    # H4: failed batches are retried (backoff), then bisected; poison writes
    # go to the dead_letters table, never dropped
    writer_max_retries: int = 2
    writer_retry_base_ms: int = 50
    writer_retry_max_ms: int = 2_000
    #: a batch failing transiently this long is re-checked against a DB ping (A2)
    writer_transient_timeout_s: float = 30.0

    # --- Event bus (P0.2) --------------------------------------------------
    event_bus: Literal["memory", "redis"] = "memory"
    redis_url: str | None = Field(default=None, validation_alias="REDIS_URL")
    redis_stream_prefix: str = "fraud"
    bus_max_retries: int = 2
    bus_partitions: int = 0
    bus_queue_size: int = 1_000
    # V6: decision side effects (cases, live feed, egress) run after the response
    write_behind_queue_size: int = 20_000
    # concurrent copilot enrichments (ŞİB drafts) — CPU work shared with scoring
    enrich_concurrency: int = 2
    # Redis idempotency window of POST /api/transactions (multi-worker replays)
    idempotency_ttl_s: int = 86_400
    # M1: lease of a PENDING claim (refreshed while the owner is scoring) and
    # how long a concurrent retry waits before answering 409 "processing"
    idempotency_pending_ttl_s: float = 45.0
    idempotency_pending_wait_ms: int = 250
    # H3: multi-worker propagation — account-status poll (DB fallback when
    # Redis is not configured) and runtime-config (thresholds/rules/models) poll
    account_status_poll_ms: int = 500
    config_poll_ms: int = 1_000
    # M13: case-outbox rows older than this are replayed by any worker
    case_outbox_replay_s: int = 120
    # slowapi storage: "memory://" (per process) or e.g. "redis://redis:6379/1"
    rate_limit_storage_uri: str = "memory://"
    bus_claim_idle_ms: int = 5_000

    # --- Feature store (P0.4) ----------------------------------------------
    feature_store: Literal["auto", "memory", "redis"] = "auto"
    feature_max_entities: int = 200_000
    # Redis feature store: TTL of payee/device first-seen keys (bounded memory),
    # per-customer lock lease and the longest wait before scoring without it.
    feature_entity_ttl_days: int = 180
    feature_lock_ttl_ms: int = 2_000
    feature_lock_wait_ms: int = 250
    # Redis Streams: "processing" lease of a message (SET NX PX) before a
    # redelivery to another consumer may start it.
    bus_processing_lease_ms: int = 30_000
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
    # PSI reference: "model" (champion validation split), "population" (the demo
    # population replayed at startup) or "auto" (population outside prod).
    drift_reference: Literal["auto", "model", "population"] = "auto"
    # Score PSI uses fixed, decision-relevant risk bands: quantile bins of a score
    # whose mass sits near 5e-5 are 1e-6 wide and alarm on meaningless jitter.
    drift_score_edges: list[float] = Field(
        default_factory=lambda: [0.01, 0.05, 0.1, 0.2, 0.35, 0.6, 0.85]
    )

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
    # A rule's HOLD floor below this risk *and* amount becomes STEP_UP (audit A4).
    rule_floor_hold_min_risk: float = 0.45
    rule_floor_hold_min_amount_try: float = 25_000.0
    #: rule-studio backtest: rows replayed at most, and wall-clock budget (s)
    rule_backtest_max_rows: int = 50_000
    rule_backtest_timeout_s: float = 15.0
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
    # single-token names: fuzzy only above this and with a matching DOB/nationality
    sanctions_single_token_threshold: float = 0.96
    sanctions_min_shared_ngrams: int = 2
    # confidence adjustment by secondary keys (match: +bonus, mismatch: x factor)
    sanctions_dob_match_bonus: float = 0.10
    sanctions_dob_mismatch_factor: float = 0.5
    sanctions_nat_match_bonus: float = 0.05
    sanctions_nat_mismatch_factor: float = 0.8
    #: hits below this confidence are not raised by the scoring engine
    sanctions_min_confidence: float = 0.45

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
    # Konsorsiyum tuzu (üyeler arası paylaşılan gizli değer; demo varsayılanı,
    # prod'da reddedilir — app/security/startup.py).
    consortium_salt: str = "anil3-consortium-demo"
    frontend_dist: Path | None = Field(default=None, validation_alias="FRONTEND_DIST")
    fp_cost_try: float = 50.0  # operasyonel maliyet: bir yanlış alarmın inceleme maliyeti
    retrain_min_new_labels: int = 50
    # --- Public-data validation (docs/VALIDATION_REPORT.md) ---------------------
    validation_dir: Path | None = None  # default: artifacts/validation
    paysim_base_date: str = "2026-01-01T00:00:00"
    #: PaySim money unit → TRY (PaySim amounts carry no currency; 1:1 keeps them as is)
    paysim_try_per_unit: float = 1.0
    #: alert budgets (share of traffic) reported by the validation
    validation_budgets: list[float] = Field(default_factory=lambda: [0.005, 0.01])
    validation_bootstrap_rounds: int = 200
    #: channels that move cash out of the bank (feature ``is_cash_channel``)
    cash_channels: list[str] = Field(default_factory=lambda: ["atm", "cash"])
    #: scored-but-unlearned events kept for late step-up / analyst feedback
    feedback_pending_max: int = 50_000
    auto_sib_case_types: list[str] = Field(default_factory=lambda: ["AML", "MULE", "YAPTIRIM"])
    online_anomaly_reason_min: float = 0.9

    # --- Case management (P0.6) ----------------------------------------------
    case_group_window_hours: int = 24
    #: A7: a TEMIZ closure releasing more than this (case total, TRY) needs a
    #: senior analyst even when the assignee closes it
    case_clean_release_senior_try: float = 50_000.0
    internal_sla_hours: int = 4
    masak_business_days: int = 10
    masak_warning_business_days: int = 2
    evidence_max_bytes: int = 1_000_000
    # Audit chain key (audit M9): when set, row hashes are HMAC-SHA256 so a DB
    # writer without the key cannot rebuild a consistent chain. Set it before the
    # first audit row is written; changing it invalidates the existing chain.
    audit_hmac_key: SecretStr = SecretStr("")
    # Resmî tatiller `holidays.Turkey` ile her yıl için dinamik hesaplanır (dini
    # bayramlar dahil). Arife yarım günleri: "business_day" (sabah çalışılır; MASAK
    # süresi yasal sınırı aşmaz) ya da "holiday". `tr_holidays`: ek kapanışlar
    # (idari izin vb.), ISO tarih listesi.
    tr_half_day_policy: Literal["business_day", "holiday"] = "business_day"
    tr_holidays: list[str] = Field(default_factory=list)

    # Yüksek riskli ülkeler: sürümlü FATF listesinden (data/jurisdictions/).
    jurisdictions_path: Path | None = None
    high_risk_lists: list[str] = Field(
        default_factory=lambda: ["call_for_action", "increased_monitoring"]
    )
    #: bankaya özgü ek yargı bölgeleri (ISO 3166-1 alpha-2, virgülle)
    high_risk_countries_extra: str = ""
    fx_rates_try: dict[str, float] = Field(default_factory=_default_fx)

    # --- Memory bounds (bug #11) --------------------------------------------
    analyzed_buffer: int = 5000
    recent_window_seconds: int = 3600
    max_tracked_customers: int = 50_000

    # --- Security ----------------------------------------------------------
    jwt_secret: SecretStr = SecretStr("")
    # SSE tickets: short-lived, single-use (EventSource cannot send headers)
    sse_ticket_ttl_s: int = 60
    # /metrics: public only if METRICS_PUBLIC=true; otherwise a bearer token
    # (METRICS_TOKEN) is required, or the endpoint answers 404 when none is set
    metrics_public: bool = False
    metrics_token: SecretStr = SecretStr("")
    # short-lived access tokens; logout revokes them earlier (jti denylist)
    jwt_ttl_minutes: int = 60
    admin_token: SecretStr = SecretStr("")
    service_api_key: SecretStr = SecretStr("")
    service_hmac_secret: SecretStr = SecretStr("")
    hmac_max_skew_seconds: int = 300
    # Demo users (analist/kidemli_analist/admin with the passwords below) are
    # seeded only when SEED_DEMO_USERS is true; unset → true outside prod, false
    # in prod. A prod process asked to seed them refuses to start.
    seed_demo_users: bool | None = None
    demo_pw_analist: SecretStr = SecretStr("analist123")
    demo_pw_kidemli: SecretStr = SecretStr("kidemli123")
    demo_pw_admin: SecretStr = SecretStr("admin123")
    cors_origins: list[str] = Field(default_factory=list)
    rate_limit_default: str = "1200/minute"
    rate_limit_login: str = "10/minute"
    # per authenticated principal (IP until authentication succeeds — see
    # principal_rate_key), not global
    rate_limit_ingest: str = "6000/minute"
    rate_limit_step_up: str = "120/minute"
    rate_limit_copilot: str = "30/minute"
    # 401 answers per client IP; beyond this every request from it gets 429
    rate_limit_auth_failures: str = "30/minute"
    # Step-up (OTP) challenge: one-time id bound to transaction + customer
    step_up_challenge_ttl_s: int = 600

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
        return self.payees_path or self.data_dir / "demo" / "payees.json"

    @property
    def resolved_customers_path(self) -> Path:
        return self.customers_path or self.data_dir / "demo" / "customers.json"

    @property
    def resolved_transactions_path(self) -> Path:
        return self.transactions_path or self.data_dir / "demo" / "transactions.json"

    @model_validator(mode="after")
    def _default_demo_users(self) -> Settings:
        if self.seed_demo_users is None:
            self.seed_demo_users = self.environment != "prod"
        return self

    @property
    def drift_population_reference(self) -> bool:
        """Build the PSI reference from the demo population (not the model's)."""
        if self.drift_reference == "auto":
            return self.environment != "prod"
        return self.drift_reference == "population"

    @property
    def high_risk_country_set(self) -> frozenset[str]:
        from app.core.jurisdictions import high_risk_codes

        return high_risk_codes()

    @property
    def resolved_validation_dir(self) -> Path:
        return self.validation_dir or BASE_DIR / "artifacts" / "validation"

    @property
    def resolved_jurisdictions_path(self) -> Path:
        return self.jurisdictions_path or BASE_DIR / "data" / "jurisdictions" / "fatf_2026-06.json"


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
