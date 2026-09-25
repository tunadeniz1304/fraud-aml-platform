"""Pydantic v2 schemas — strict validation for the dashboard API and events."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CHARS = (
    "transaction_id",
    "customer_id",
    "device_id",
    "location",
    "purpose",
    "beneficiary_id",
    "ip_address",
    "channel",
    "currency",
    "country",
    "beneficiary_iban",
    "beneficiary_name",
)


class SessionIn(BaseModel):
    """Optional device/session/behavioural-biometrics block (P1.4, simulated SDK)."""

    model_config = ConfigDict(extra="forbid")

    device_fingerprint: str | None = Field(default=None, max_length=128)
    is_emulator: bool = False
    is_rooted: bool = False
    remote_access_tool: bool = False
    active_call: bool = False
    ip_asn: int | None = Field(default=None, ge=0, le=4_294_967_295)
    is_vpn_or_tor: bool | None = None
    typing_cadence_ms: float | None = Field(default=None, ge=0, le=5_000)
    paste_used: bool = False
    session_duration_s: float | None = Field(default=None, ge=0, le=86_400)
    login_to_transfer_s: float | None = Field(default=None, ge=0, le=86_400)


class TransactionIn(BaseModel):
    """Incoming transaction (strict, extra forbidden, control chars stripped)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transaction_id: str = Field(min_length=1, max_length=64)
    ts: datetime
    customer_id: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=100_000_000)
    currency: str = Field(min_length=2, max_length=3)
    device_id: str = Field(min_length=1, max_length=64)
    location: str = Field(min_length=1, max_length=128)
    beneficiary_id: str = Field(default="", max_length=64)
    ip_address: str = Field(default="", max_length=45)
    channel: str = Field(default="web", pattern=r"^(mobile|web|atm)$")
    country: str = Field(default="", min_length=2, max_length=2)
    purpose: str = Field(default="", max_length=160)
    beneficiary_iban: str = Field(
        default="", max_length=34, pattern=r"^([A-Z]{2}\d{2}[A-Z0-9]{10,30})?$"
    )
    beneficiary_name: str = Field(default="", max_length=160)
    session: SessionIn | None = None

    @field_validator("currency", "country")
    @classmethod
    def upper_(cls, v: str) -> str:
        return v.upper()

    @field_validator(*_CHARS)
    @classmethod
    def strip_ctrl(cls, v: str) -> str:
        return _CTRL_RE.sub("", v).strip()


class AnalyzedTransactionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_id: str
    customer_id: str
    amount: float
    currency: str
    device_id: str
    location: str
    ts: datetime
    amount_try: float | None = None
    channel: str | None = None
    country: str | None = None
    purpose: str | None = None
    beneficiary_id: str | None = None
    risk_score: float
    decision: str | None = None
    decision_legacy: str | None = None
    decision_reason: str | None = None
    components: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[dict[str, Any]] = Field(default_factory=list)
    rule_hits: list[dict[str, Any]] = Field(default_factory=list)
    risk_explanation: list[str] = Field(default_factory=list)
    case_required: bool = False
    hold_minutes: int | None = None
    rule_version: str | None = None
    model_version: str | None = None
    challenger_version: str | None = None
    challenger_score: float | None = None
    latency_ms: float | None = None
    high_risk_country: bool = False
    peer_group_avg: float | None = None
    peer_amount_ratio: float | None = None
    microcluster: float = 0.0
    mule_score: float = 0.0
    mule_signals: list[str] = Field(default_factory=list)
    sanctions: list[dict[str, Any]] = Field(default_factory=list)
    sanctions_hit: bool = False
    force_review: bool = False
    unknown_customer: bool = False
    account_blocked: bool = False
    duplicate: bool = False


class BlockOut(BaseModel):
    transaction_id: str
    customer_id: str
    risk_score: float
    blocked_at: datetime
    reason: str


class AccountOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    name: str
    hesap_durumu: str
    updated_at: datetime


class AuditRowOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    created_at: datetime
    transaction_id: str
    customer_id: str
    risk_score: float
    decision: str
    reason: str
    actor: str = "system"
    event_type: str = "DECISION"
    hash: str = ""


class PipelineStatusOut(BaseModel):
    total_monitored: int
    rejected: int = 0
    analyzed: int
    blocked: int
    warned: int
    passed: int
    llm_mode: str
    vector_customers: int
    accounts: list[AccountOut]


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token tipi, sır değil
    expires_in: int
    role: str
    display_name: str


class LLMStatusOut(BaseModel):
    mode: str
    model: str
    base_url_host: str | None = None
    key_present: bool
    last_latency_ms: float | None = None
    calls: int
    failures: int
    last_error_kind: str | None = None
