"""Relational data model (P0.3).

PostgreSQL in production (asyncpg), SQLite in tests/local runs (aiosqlite);
schema managed by Alembic. Money is ``NUMERIC(18,2)`` with a currency column
plus the TRY-normalised amount. ``audit_log`` is hash-chained: every row stores
the SHA-256 of its predecessor so any tampering is detectable
(``GET /api/audit/verify``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDoc, Money, UTCDateTime, utcnow


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    home_city: Mapped[str] = mapped_column(String(80), default="")
    home_country: Mapped[str] = mapped_column(String(2), default="TR")
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vulnerable: Mapped[bool] = mapped_column(Boolean, default=False)
    iban: Mapped[str | None] = mapped_column(String(34), nullable=True, index=True)
    segment: Mapped[str] = mapped_column(String(32), default="bireysel")
    profile: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Account(Base):
    __tablename__ = "accounts"

    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), default="AKTIF")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AccountStatusHistory(Base):
    __tablename__ = "account_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="TRY")
    amount_try: Mapped[Decimal] = mapped_column(Money)
    channel: Mapped[str] = mapped_column(String(16), default="web")
    device_id: Mapped[str] = mapped_column(String(64), default="")
    ip_address: Mapped[str] = mapped_column(String(45), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    country: Mapped[str] = mapped_column(String(2), default="")
    beneficiary_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    beneficiary_iban: Mapped[str] = mapped_column(String(34), default="")
    beneficiary_name: Mapped[str] = mapped_column(String(160), default="")
    purpose: Mapped[str] = mapped_column(String(160), default="")
    session: Mapped[dict[str, Any] | None] = mapped_column(JSONDoc, nullable=True)
    #: SHA-256 of the canonical request payload (same id + other payload -> 409)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_customer_created", "customer_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(String(64), unique=True)
    customer_id: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(12), index=True)
    risk_score: Mapped[float] = mapped_column(Float)
    components: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    reason_codes: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc, default=list)
    features: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    rule_version: Mapped[str] = mapped_column(String(32), default="")
    model_version: Mapped[str] = mapped_column(String(32), default="")
    challenger_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    challenger_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="FINAL")
    hold_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # A11: one alert per transaction (outbox replay / redelivery is a no-op)
    transaction_id: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    alert_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(12), default="medium")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    amount_try: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), server_default="0")
    decision: Mapped[str] = mapped_column(String(12), default="", server_default="")
    reason_codes: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc, default=list)
    case_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    case_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    status: Mapped[str] = mapped_column(String(20), default="YENI", index=True)
    alert_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    assigned_to: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ring_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    total_amount_try: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    suspicion_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    masak_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    internal_sla_due: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sib_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sib_draft: Mapped[dict[str, Any] | None] = mapped_column(JSONDoc, nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONDoc, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class CaseEvent(Base):
    __tablename__ = "case_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="system")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Label(Base):
    __tablename__ = "labels"
    __table_args__ = (UniqueConstraint("transaction_id", "source", name="uq_labels_tx_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32), default="analyst")
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    when_expr: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    reason_code: Mapped[str] = mapped_column(String(64))
    reason_template: Mapped[str] = mapped_column(Text, default="")
    action_hint: Mapped[str | None] = mapped_column(String(12), nullable=True)
    severity: Mapped[str] = mapped_column(String(12), default="medium")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version", name="uq_rule_versions_rule_ver"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONDoc)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ModelVersion(Base):
    __tablename__ = "models"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="lightgbm")
    status: Mapped[str] = mapped_column(String(16), default="challenger")
    path: Mapped[str] = mapped_column(String(255))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Approval(Base):
    """Maker-checker requests (unblock, ŞİB approval, model promotion)."""

    __tablename__ = "approvals"
    # at most one pending request per (kind, target): race-safe duplicate guard
    __table_args__ = (
        Index(
            "uq_approvals_pending_target",
            "kind",
            "target_id",
            unique=True,
            sqlite_where=text("status = 'BEKLIYOR'"),
            postgresql_where=text("status = 'BEKLIYOR'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="BEKLIYOR", index=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class GraphRing(Base):
    __tablename__ = "graph_rings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    members: Mapped[list[str]] = mapped_column(JSONDoc, default=list)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    event_type: Mapped[str] = mapped_column(String(32))
    entity_type: Mapped[str] = mapped_column(String(32), default="transaction")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class DeadLetterRecord(Base):
    """Write that could not be persisted after retries and bisection (H4)."""

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(32), default="writer")
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class RuntimeConfig(Base):
    """Versioned runtime configuration shared by every worker (H3):
    policy thresholds and the rule / champion-model generation counters."""

    __tablename__ = "runtime_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CaseOutbox(Base):
    """Durable case-intake outbox (M13): written in the decision's own batch,
    deleted once the alert/case exists; stale rows are replayed."""

    __tablename__ = "case_outbox"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDoc, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
