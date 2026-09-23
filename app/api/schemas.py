"""Pydantic v2 schemas — strict validation for the dashboard API and events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionIn(BaseModel):
    """Incoming transaction payload (strict, extra fields rejected)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transaction_id: str = Field(min_length=1)
    ts: datetime
    customer_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    currency: str = Field(min_length=2, max_length=3)
    device_id: str = Field(min_length=1)
    location: str = Field(min_length=1)
    country: str = Field(default="", min_length=2, max_length=2)
    purpose: str = Field(default="", max_length=120)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("country")
    @classmethod
    def country_upper(cls, v: str) -> str:
        return v.upper()


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
