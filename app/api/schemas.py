"""Pydantic v2 schemas — strict validation for the dashboard API and events."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CHARS = (
    "transaction_id", "customer_id", "device_id", "location", "purpose",
    "beneficiary_id", "ip_address", "channel", "currency", "country",
)


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

    @field_validator("currency", "country")
    @classmethod
    def upper_(cls, v: str) -> str:
        return v.upper()

    @field_validator(*_CHARS)
    @classmethod
    def strip_ctrl(cls, v: str) -> str:
        return _CTRL_RE.sub("", v).strip()


class RiskFactorsOut(BaseModel):
    amount: float
    device: float
    location: float
    time: float
    velocity: float


class AnalyzedTransactionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_id: str
    customer_id: str
    amount: float
    currency: str
    device_id: str
    location: str
    ts: datetime
    risk_score: float
    risk_score_rule: float | None = None
    semantic_distance: float | None = None
    risk_factors: RiskFactorsOut | None = None
    risk_explanation: list[str] = Field(default_factory=list)
    high_risk_country: bool = False
    peer_group_avg: float | None = None
    peer_amount_ratio: float | None = None
    microcluster: float = 0.0
    mule_score: float = 0.0
    mule_signals: list[str] = Field(default_factory=list)
    sanctions: list[dict[str, Any]] = Field(default_factory=list)
    sanctions_hit: bool = False
    llm: dict[str, Any] | None = None


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


class PipelineStatusOut(BaseModel):
    total_monitored: int
    analyzed: int
    blocked: int
    warned: int
    passed: int
    llm_provider: str
    vector_customers: int
    accounts: list[AccountOut]
