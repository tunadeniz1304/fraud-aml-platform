"""Case management (P0.6): alerts → cases, SLA, assignment, decisions, maker-checker.

* Every HOLD / BLOCK decision (or one that the policy marks ``case_required``,
  e.g. a sanctions hit) becomes an **alert**. Alerts of the same customer — or
  the same mule ring — within ``CASE_GROUP_WINDOW_HOURS`` are grouped into one
  open **case**; priority = Σ risk × amount (TRY).
* Each case carries the internal SLA (first action within 4 h) and the MASAK
  deadline (10 business days from suspicion).
* Analyst decisions close the case and become **labels** (feedback loop), and
  release or reject held payments.
* **Maker-checker**: lifting a block, approving a ŞİB (and promoting a model)
  are *requests* that a second, different user with at least the
  ``kidemli_analist`` role must approve.

All mutations are durable before returning and are written to the
hash-chained audit log.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases import sla
from app.config import get_settings
from app.db import repository as repo
from app.db.audit import AuditEntry
from app.db.base import to_money, utcnow
from app.db.database import Database
from app.db.models import Alert, Approval, Case, CaseEvent, Decision, Label, Transaction
from app.db.writer import PersistenceWriter
from app.monitoring import metrics
from app.security.auth import ROLE_RANK
from app.services.accounts import AccountNotFoundError, AccountService
from app.services.config_sync import ConfigConflict

logger = logging.getLogger("fraud.cases")


def sib_draft_hash(draft: dict[str, Any] | None) -> str:
    """SHA-256 of the canonical JSON of a ŞİB draft (A6).

    Stored in the SIB approval payload at request time, so the approver signs
    off exactly the text that is submitted to MASAK.
    """
    blob = json.dumps(
        draft or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


OPEN_STATUSES = ("YENI", "INCELENIYOR", "BEKLEMEDE")
CLOSED_STATUSES = ("KAPANDI_FRAUD", "KAPANDI_TEMIZ", "SIB_GONDERILDI")
STATUSES = OPEN_STATUSES + CLOSED_STATUSES
MANUAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "YENI": ("INCELENIYOR", "BEKLEMEDE"),
    "INCELENIYOR": ("BEKLEMEDE",),
    "BEKLEMEDE": ("INCELENIYOR",),
    "KAPANDI_FRAUD": ("INCELENIYOR",),  # yeniden açma
    "KAPANDI_TEMIZ": ("INCELENIYOR",),
}
OUTCOMES = {"FRAUD": ("KAPANDI_FRAUD", 1), "TEMIZ": ("KAPANDI_TEMIZ", 0)}
APPROVAL_KINDS = ("UNBLOCK", "SIB", "MODEL_PROMOTE", "RULE_CHANGE", "POLICY_THRESHOLDS")
#: least privileged role that may decide each approval kind (unknown kinds: admin)
APPROVAL_MIN_ROLE: dict[str, str] = {
    "UNBLOCK": "kidemli_analist",
    "SIB": "kidemli_analist",
    "MODEL_PROMOTE": "admin",
    "RULE_CHANGE": "kidemli_analist",
    "POLICY_THRESHOLDS": "admin",
}

#: rule tag -> alert type (the tag carried by the strongest fired rule wins;
#: ties follow this order)
_TYPE_BY_TAG = (
    ("mule", "MULE"),
    ("aml", "AML"),
    ("ato", "ATO"),
    ("app", "APP"),
    ("card", "CARD_TESTING"),
)
#: typology tags that describe the pattern itself and win over generic
#: account-takeover evidence (card testing often runs from a fresh bot device)
_SPECIFIC_TAGS = ("card",)
_TYPE_RANK = ["DAVRANIS", "ATO", "CARD_TESTING", "APP", "MULE", "AML", "YAPTIRIM"]
#: pre-v2 names still present in older databases
LEGACY_TYPES = {"KART_TESTI": "CARD_TESTING"}
TYPE_TEXT = {
    "YAPTIRIM": "Yaptırım/PEP eşleşmesi",
    "AML": "Kara para aklama şüphesi (parçalama)",
    "MULE": "Para katırı (mule) ağı şüphesi",
    "APP": "Yetkili itme ödemesi (APP) dolandırıcılığı şüphesi",
    "ATO": "Hesap ele geçirme (ATO) şüphesi",
    "CARD_TESTING": "Kart testi şüphesi",
    "DAVRANIS": "Davranışsal anomali",
    "BILINMEYEN_MUSTERI": "Bilinmeyen müşteri",
}


class CaseError(ValueError):
    """Business-rule violation (maps to HTTP 409/422)."""


class CaseNotFoundError(KeyError):
    pass


class MakerCheckerError(PermissionError):
    """The approver is the requester (four-eyes principle)."""


class ApprovalUnavailableError(RuntimeError):
    """The approval could not be applied for an infrastructure reason (the
    database is unavailable); nothing was applied and it can be retried (503)."""


AfterCommit = Callable[[], Awaitable[None] | None]


@dataclass
class ApprovalTx:
    """The unit of work of one maker-checker decision (A5).

    A handler performs its database writes (rule row, ``runtime_config``
    publish, account status, ŞİB state) in :attr:`session` — the same
    transaction that flips the approval status — collects its audit entries
    in :attr:`audits` (appended to the hash chain in that transaction) and
    registers in-memory effects (engine swap, caches) with :meth:`on_commit`;
    they run only after the commit. :meth:`on_rollback` undoes a
    non-transactional side effect (the model registry file) if the
    transaction does not commit. Either everything is applied or nothing is,
    and a failed approval stays ``BEKLIYOR`` for a retry.
    """

    session: AsyncSession
    audits: list[AuditEntry] = field(default_factory=list)
    after_commit: list[AfterCommit] = field(default_factory=list)
    after_rollback: list[Callable[[], None]] = field(default_factory=list)

    def on_commit(self, fn: AfterCommit) -> None:
        self.after_commit.append(fn)

    def on_rollback(self, fn: Callable[[], None]) -> None:
        self.after_rollback.append(fn)


ApprovalHandler = Callable[[dict[str, Any], str, ApprovalTx], Awaitable[dict[str, Any]]]


def _require_senior(role: str | None, action: str) -> None:
    if role is not None and ROLE_RANK.get(role, -1) < ROLE_RANK["kidemli_analist"]:
        raise MakerCheckerError(f"{action} en az kıdemli analist yetkisi gerektirir")


def alert_type(event: dict[str, Any]) -> str:
    if event.get("sanctions_hit"):
        return "YAPTIRIM"
    if event.get("unknown_customer"):
        return "BILINMEYEN_MUSTERI"
    strength: dict[str, float] = {}
    for hit in event.get("rule_hits") or []:
        for tag in hit.get("tags") or []:
            strength[tag] = max(strength.get(tag, 0.0), float(hit.get("score") or 0.0))
    for tag, kind in _TYPE_BY_TAG:
        if tag in _SPECIFIC_TAGS and tag in strength:
            return kind
    best: tuple[float, int, str] | None = None
    for order, (tag, kind) in enumerate(_TYPE_BY_TAG):
        if tag in strength:
            candidate = (strength[tag], -order, kind)
            best = candidate if best is None or candidate > best else best
    return best[2] if best else "DAVRANIS"


def _severity(event: dict[str, Any]) -> str:
    if event.get("sanctions_hit") or event.get("decision") == "BLOCK":
        return "critical"
    return "high" if event.get("decision") == "HOLD" else "medium"


def _decided_at(event: dict[str, Any], now: datetime) -> datetime:
    """A11: when the decision was taken (``decided_at`` on the DECIDED event,
    also kept in the case outbox), so a replayed intake does not start the
    MASAK deadline late. Falls back to ``now``; never in the future."""
    raw = event.get("decided_at")
    value: datetime | None = None
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if value is None:
        return now
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return min(value.astimezone(UTC), now)


def _rank(kind: str) -> int:
    kind = LEGACY_TYPES.get(kind, kind)
    return _TYPE_RANK.index(kind) if kind in _TYPE_RANK else 0


class CaseService:
    def __init__(
        self,
        db: Database,
        *,
        accounts: AccountService | None = None,
        writer: PersistenceWriter | None = None,
        customer_name: Callable[[str], str] | None = None,
        on_fraud_confirmed: Callable[[str, list[str]], None] | None = None,
        on_fraud_case: Callable[[dict[str, Any]], None] | None = None,
        on_labelled: Callable[[list[str], str], Awaitable[None]] | None = None,
    ) -> None:
        self.on_fraud_case = on_fraud_case
        #: profile feedback hook: labelled transaction ids + "clean" / "fraud"
        self.on_labelled = on_labelled
        self.db = db
        #: feedback hook (graph risk propagation): customer id + beneficiary accounts
        self.on_fraud_confirmed = on_fraud_confirmed
        self.accounts = accounts
        self.writer = writer
        self.customer_name = customer_name or (lambda cid: cid)
        self._lock = asyncio.Lock()
        self.handlers: dict[str, ApprovalHandler] = {
            "UNBLOCK": self._approve_unblock,
            "SIB": self._approve_sib,
        }

    # --- helpers ---------------------------------------------------------------------
    async def _audit(
        self, event: str, entity: str, actor: str, reason: str, **payload: Any
    ) -> None:
        entry = AuditEntry(
            event_type=event,
            entity_type="case" if event.startswith("CASE") else "approval",
            entity_id=entity,
            actor=actor,
            reason=reason,
            customer_id=payload.pop("customer_id", None),
            payload=payload,
        )
        if self.writer is not None:
            await self.writer.record_audit(entry)

    @staticmethod
    def _event(case_id: int, kind: str, actor: str, **payload: Any) -> CaseEvent:
        return CaseEvent(case_id=case_id, event_type=kind, actor=actor, payload=payload)

    @staticmethod
    async def _case(session: AsyncSession, case_id: int) -> Case:
        case = await session.get(Case, case_id)
        if case is None:
            raise CaseNotFoundError(case_id)
        return case

    # --- alert intake (pipeline subscriber) --------------------------------------------
    def needs_alert(self, event: dict[str, Any]) -> bool:
        if event.get("account_blocked"):
            return False  # already blocked: the existing case covers it
        return bool(event.get("case_required")) or event.get("decision") in ("HOLD", "BLOCK")

    async def on_decision(self, event: dict[str, Any]) -> int | None:
        """Create an alert and attach it to an open case (or open one).

        A11: idempotent per transaction — a transaction that already has an
        alert (outbox replay, redelivery) is skipped and ``None`` returned; the
        unique ``alerts.transaction_id`` index rolls back a concurrent second
        intake on another worker. The case clock starts at the decision time.
        """
        if not self.needs_alert(event):
            return None
        try:
            return await self._intake(event)
        except IntegrityError:
            if await self._has_alert(str(event["transaction_id"])):
                logger.info("[Cases] %s zaten kayıtlı — tekrar atlandı", event["transaction_id"])
                return None
            raise

    async def _has_alert(self, tx_id: str) -> bool:
        async with self.db.session() as session:
            found = await session.execute(
                select(Alert.id).where(Alert.transaction_id == tx_id).limit(1)
            )
            return found.first() is not None

    async def _intake(self, event: dict[str, Any]) -> int | None:
        kind = alert_type(event)
        risk = float(event.get("risk_score") or 0.0)
        amount = to_money(event.get("amount_try") or event.get("amount") or 0)
        customer_id = str(event["customer_id"])
        ring_id = event.get("ring_id")
        tx_id = str(event["transaction_id"])
        now = utcnow()
        decided = _decided_at(event, now)
        window = now - timedelta(hours=get_settings().case_group_window_hours)
        async with self._lock, self.db.transaction() as session:
            duplicate = await session.execute(
                select(Alert.id).where(Alert.transaction_id == tx_id).limit(1)
            )
            if duplicate.first() is not None:
                logger.info("[Cases] %s zaten kayıtlı — tekrar atlandı", tx_id)
                return None
            query = select(Case).where(Case.status.in_(OPEN_STATUSES), Case.updated_at >= window)
            if ring_id:
                query = query.where((Case.customer_id == customer_id) | (Case.ring_id == ring_id))
            else:
                query = query.where(Case.customer_id == customer_id)
            case = (
                (await session.execute(query.order_by(Case.id.desc()).limit(1))).scalars().first()
            )
            created = case is None
            if case is None:
                case = Case(
                    customer_id=customer_id,
                    case_type=kind,
                    title=f"{TYPE_TEXT.get(kind, kind)} — {self.customer_name(customer_id)}",
                    status="YENI",
                    priority=0.0,
                    ring_id=ring_id,
                    total_amount_try=Decimal("0"),
                    suspicion_at=decided,
                    masak_deadline=sla.masak_deadline(decided),
                    internal_sla_due=sla.internal_sla_due(decided),
                    alert_count=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(case)
                await session.flush()
                session.add(self._event(case.id, "OPENED", "system", alert_type=kind))
            elif _rank(kind) > _rank(case.case_type):
                case.case_type = kind
                case.title = f"{TYPE_TEXT.get(kind, kind)} — {self.customer_name(customer_id)}"
            if ring_id and not case.ring_id:
                case.ring_id = ring_id
            alert = Alert(
                transaction_id=str(event["transaction_id"]),
                customer_id=customer_id,
                alert_type=kind,
                severity=_severity(event),
                risk_score=risk,
                amount_try=amount,
                decision=str(event.get("decision") or ""),
                reason_codes=list(event.get("reason_codes") or [])[:5],
                case_id=case.id,
                created_at=now,
            )
            session.add(alert)
            case.alert_count = (case.alert_count or 0) + 1
            case.total_amount_try = (case.total_amount_try or Decimal("0")) + amount
            case.priority = round(float(case.priority or 0.0) + risk * float(amount), 2)
            case.updated_at = now
            session.add(
                self._event(
                    case.id,
                    "ALERT_ADDED",
                    "system",
                    transaction_id=str(event["transaction_id"]),
                    alert_type=kind,
                    risk_score=risk,
                    decision=event.get("decision"),
                    ring_id=ring_id,
                    app_warning=event.get("app_warning"),
                )
            )
            case_id = case.id
        metrics.ALERTS.labels(alert_type=kind).inc()
        if created:
            metrics.CASES_OPENED.labels(case_type=kind).inc()
            await self._audit(
                "CASE_OPENED",
                str(case_id),
                "system",
                f"Vaka açıldı: {TYPE_TEXT.get(kind, kind)}",
                customer_id=customer_id,
                transaction_id=str(event["transaction_id"]),
            )
        return case_id

    # --- queries ---------------------------------------------------------------------------
    @staticmethod
    def _summary(case: Case, now: datetime) -> dict[str, Any]:
        sla_due = case.internal_sla_due
        return {
            "id": case.id,
            "customer_id": case.customer_id,
            "case_type": case.case_type,
            "title": case.title,
            "status": case.status,
            "priority": case.priority,
            "assigned_to": case.assigned_to,
            "ring_id": case.ring_id,
            "alert_count": case.alert_count,
            "total_amount_try": float(case.total_amount_try or 0),
            "suspicion_at": case.suspicion_at,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "closed_at": case.closed_at,
            "decision": case.decision,
            "sib_status": case.sib_status,
            "internal_sla_due": sla_due,
            "internal_sla_breached": bool(
                sla_due and case.status == "YENI" and now > sla_due and not case.assigned_to
            ),
            "sla_remaining_s": int((sla_due - now).total_seconds()) if sla_due else None,
            "masak_deadline": case.masak_deadline,
            "masak_business_days_left": sla.business_days_between(now, case.masak_deadline)
            if case.masak_deadline
            else None,
        }

    async def list_cases(
        self,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        customer_id: str | None = None,
        case_type: str | None = None,
        ring_id: str | None = None,
        order: str = "priority",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = self._filtered(
            select(Case),
            status=status,
            assigned_to=assigned_to,
            customer_id=customer_id,
            case_type=case_type,
            ring_id=ring_id,
        )
        ordering: tuple[Any, ...] = {
            "priority": (Case.priority.desc(), Case.id.desc()),
            "sla": (Case.internal_sla_due.asc(), Case.id.asc()),
            "masak": (Case.masak_deadline.asc(), Case.id.asc()),
            "recent": (Case.updated_at.desc(), Case.id.desc()),
        }.get(order, (Case.priority.desc(), Case.id.desc()))
        async with self.db.session() as session:
            rows = (
                await session.execute(query.order_by(*ordering).offset(offset).limit(limit))
            ).scalars()
            now = utcnow()
            return [self._summary(c, now) for c in rows]

    async def count_cases(self, **filters: str | None) -> int:
        query = self._filtered(select(func.count(Case.id)), **filters)
        async with self.db.session() as session:
            return int((await session.execute(query)).scalar_one())

    @staticmethod
    def _filtered(
        query: Any,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        customer_id: str | None = None,
        case_type: str | None = None,
        ring_id: str | None = None,
    ) -> Any:
        if status == "OPEN":
            query = query.where(Case.status.in_(OPEN_STATUSES))
        elif status:
            query = query.where(Case.status == status)
        if assigned_to:
            query = query.where(Case.assigned_to == assigned_to)
        if customer_id:
            query = query.where(Case.customer_id == customer_id)
        if case_type:
            query = query.where(Case.case_type == case_type)
        if ring_id:
            query = query.where(Case.ring_id == ring_id)
        return query

    async def get_case(self, case_id: int) -> dict[str, Any]:
        async with self.db.session() as session:
            case = await self._case(session, case_id)
            alerts = (
                (
                    await session.execute(
                        select(Alert).where(Alert.case_id == case_id).order_by(Alert.id)
                    )
                )
                .scalars()
                .all()
            )
            tx_ids = [a.transaction_id for a in alerts]
            txs = {
                t.id: t
                for t in (
                    await session.execute(select(Transaction).where(Transaction.id.in_(tx_ids)))
                ).scalars()
            }
            decisions = {
                d.transaction_id: d
                for d in (
                    await session.execute(
                        select(Decision).where(Decision.transaction_id.in_(tx_ids))
                    )
                ).scalars()
            }
            events = (
                (
                    await session.execute(
                        select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.id)
                    )
                )
                .scalars()
                .all()
            )
            approvals = (
                (
                    await session.execute(
                        select(Approval)
                        .where(
                            (Approval.target_id == str(case_id)) & (Approval.kind == "SIB")
                            | (Approval.target_id == case.customer_id)
                            & (Approval.kind == "UNBLOCK")
                        )
                        .order_by(Approval.id)
                    )
                )
                .scalars()
                .all()
            )
            out = self._summary(case, utcnow())
            out["summary"] = case.summary
            out["sib_draft"] = case.sib_draft
            out["alerts"] = [
                {
                    "id": a.id,
                    "transaction_id": a.transaction_id,
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "risk_score": a.risk_score,
                    "amount_try": float(a.amount_try or 0),
                    "decision": a.decision,
                    "reason_codes": a.reason_codes,
                    "created_at": a.created_at,
                    "transaction": _tx_dict(txs.get(a.transaction_id)),
                    "scoring": _decision_dict(decisions.get(a.transaction_id)),
                }
                for a in alerts
            ]
            out["events"] = [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "actor": e.actor,
                    "payload": {k: v for k, v in (e.payload or {}).items() if k != "content_b64"},
                    "created_at": e.created_at,
                }
                for e in events
            ]
            out["approvals"] = [_approval_dict(a) for a in approvals]
            return out

    async def list_alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.execute(select(Alert).order_by(Alert.id.desc()).limit(limit))
            ).scalars()
            return [
                {
                    "id": a.id,
                    "transaction_id": a.transaction_id,
                    "customer_id": a.customer_id,
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "risk_score": a.risk_score,
                    "amount_try": float(a.amount_try or 0),
                    "decision": a.decision,
                    "case_id": a.case_id,
                    "created_at": a.created_at,
                }
                for a in rows
            ]

    async def counts(self) -> dict[str, int]:
        async with self.db.session() as session:
            rows = await session.execute(select(Case.status, func.count()).group_by(Case.status))
            return {status: int(n) for status, n in rows.all()}

    # --- analyst actions -----------------------------------------------------------------
    async def assign(self, case_id: int, assignee: str, actor: str) -> dict[str, Any]:
        async with self.db.transaction() as session:
            case = await self._case(session, case_id)
            if case.status in CLOSED_STATUSES:
                raise CaseError("kapalı vaka atanamaz")
            previous = case.assigned_to
            case.assigned_to = assignee
            if case.status == "YENI":
                case.status = "INCELENIYOR"
            case.updated_at = utcnow()
            session.add(
                self._event(case_id, "ASSIGNED", actor, assignee=assignee, previous=previous)
            )
            customer = case.customer_id
        await self._audit(
            "CASE_ASSIGNED",
            str(case_id),
            actor,
            f"Vaka {assignee} kullanıcısına atandı",
            customer_id=customer,
        )
        return await self.get_case(case_id)

    async def set_status(
        self, case_id: int, status: str, actor: str, note: str = "", *, role: str | None = None
    ) -> dict[str, Any]:
        async with self.db.transaction() as session:
            case = await self._case(session, case_id)
            if case.status in CLOSED_STATUSES:
                _require_senior(role, "kapalı bir vakayı yeniden açmak")
            allowed = MANUAL_TRANSITIONS.get(case.status, ())
            if status not in allowed:
                raise CaseError(
                    f"{case.status} → {status} geçişi yapılamaz"
                    + (f" (izinli: {', '.join(allowed)})" if allowed else "")
                    + ("; kapatmak için karar verin" if status in CLOSED_STATUSES else "")
                )
            previous = case.status
            case.status = status
            if previous in CLOSED_STATUSES:
                case.closed_at = None
                case.decision = None
            case.updated_at = utcnow()
            session.add(self._event(case_id, "STATUS", actor, frm=previous, to=status, note=note))
            customer = case.customer_id
        await self._audit(
            "CASE_STATUS",
            str(case_id),
            actor,
            f"Vaka durumu {previous} → {status}",
            customer_id=customer,
        )
        return await self.get_case(case_id)

    async def add_note(self, case_id: int, text: str, actor: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise CaseError("not boş olamaz")
        async with self.db.transaction() as session:
            case = await self._case(session, case_id)
            case.updated_at = utcnow()
            event = self._event(case_id, "NOTE", actor, text=text)
            session.add(event)
            await session.flush()
            note = {"id": event.id, "text": text, "actor": actor, "created_at": event.created_at}
        return note

    async def record_event(self, case_id: int, kind: str, actor: str, **payload: Any) -> None:
        """Append a system event (e.g. ``COPILOT_ERROR``) to the case timeline."""
        async with self.db.transaction() as session:
            await self._case(session, case_id)
            session.add(self._event(case_id, kind, actor, **payload))

    async def add_evidence(
        self,
        case_id: int,
        *,
        filename: str,
        content_type: str,
        content_b64: str,
        note: str,
        actor: str,
    ) -> dict[str, Any]:
        try:
            content = base64.b64decode(content_b64, validate=True)
        except ValueError:
            raise CaseError("kanıt içeriği geçerli base64 değil") from None
        if len(content) > get_settings().evidence_max_bytes:
            raise CaseError("kanıt dosyası çok büyük")
        digest = hashlib.sha256(content).hexdigest()
        async with self.db.transaction() as session:
            case = await self._case(session, case_id)
            case.updated_at = utcnow()
            event = self._event(
                case_id,
                "EVIDENCE",
                actor,
                filename=filename,
                content_type=content_type,
                size=len(content),
                sha256=digest,
                note=note,
                content_b64=content_b64,
            )
            session.add(event)
            await session.flush()
            event_id = event.id
            customer = case.customer_id
        await self._audit(
            "CASE_EVIDENCE",
            str(case_id),
            actor,
            f"Kanıt eklendi: {filename} (sha256 {digest[:12]}…)",
            customer_id=customer,
            sha256=digest,
        )
        return {"id": event_id, "filename": filename, "size": len(content), "sha256": digest}

    async def evidence(self, case_id: int, evidence_id: int) -> dict[str, Any]:
        async with self.db.session() as session:
            event = await session.get(CaseEvent, evidence_id)
            if event is None or event.case_id != case_id or event.event_type != "EVIDENCE":
                raise CaseNotFoundError(evidence_id)
            return dict(event.payload)

    async def decide(
        self, case_id: int, outcome: str, actor: str, note: str = "", *, role: str | None = None
    ) -> dict[str, Any]:
        """Close the case as FRAUD / TEMIZ, write labels, settle held payments.

        A FRAUD closure blocks payments, feeds the model labels and opens the
        ŞİB path, so it needs at least ``kidemli_analist`` (``role`` is the
        caller's role; internal callers pass ``None``).
        """
        if outcome not in OUTCOMES:
            raise CaseError("karar FRAUD veya TEMIZ olmalı")
        if outcome == "FRAUD":
            _require_senior(role, "vakayı FRAUD olarak kapatmak")
        status, label = OUTCOMES[outcome]
        now = utcnow()
        async with self.db.transaction() as session:
            case = await self._case(session, case_id)
            if case.status in CLOSED_STATUSES:
                raise CaseError("vaka zaten kapalı")
            tx_ids = list(
                (
                    await session.execute(
                        select(Alert.transaction_id).where(Alert.case_id == case_id)
                    )
                )
                .scalars()
                .all()
            )
            for tx_id in dict.fromkeys(tx_ids):
                stmt = repo._insert(session, Label).values(
                    transaction_id=tx_id,
                    label=label,
                    source="analyst",
                    case_id=case_id,
                    created_by=actor,
                    created_at=now,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["transaction_id", "source"],
                        set_={"label": label, "case_id": case_id, "created_by": actor},
                    )
                )
            await session.execute(
                update(Decision)
                .where(Decision.transaction_id.in_(tx_ids), Decision.status == "BEKLEMEDE")
                .values(status="REDDEDILDI" if label else "SERBEST")
            )
            case.status = status
            case.decision = outcome
            case.closed_at = now
            case.updated_at = now
            case.assigned_to = case.assigned_to or actor
            session.add(
                self._event(
                    case_id, "DECISION", actor, outcome=outcome, note=note, labels=len(tx_ids)
                )
            )
            customer = case.customer_id
        metrics.CASE_DECISIONS.labels(outcome=outcome).inc()
        if outcome == "FRAUD" and self.on_fraud_confirmed is not None:
            async with self.db.session() as session:
                rows = await session.execute(
                    select(Transaction.beneficiary_iban, Transaction.beneficiary_id).where(
                        Transaction.id.in_(tx_ids)
                    )
                )
                beneficiaries = [value for pair in rows.all() for value in pair if value]
            self.on_fraud_confirmed(customer, sorted(set(beneficiaries)))
        await self._settle_account(customer, outcome, actor, case_id)
        if self.on_labelled is not None:
            await self.on_labelled(list(tx_ids), "fraud" if label else "clean")
        if outcome == "FRAUD" and self.on_fraud_case is not None:
            try:
                self.on_fraud_case(await self.get_case(case_id))
            except Exception:  # RAG indexing must not break the decision
                logger.exception("[Cases] vaka RAG deposuna eklenemedi (#%s)", case_id)
        await self._audit(
            "CASE_DECISION",
            str(case_id),
            actor,
            f"Vaka kararı: {outcome} ({len(tx_ids)} işlem etiketlendi)",
            customer_id=customer,
            outcome=outcome,
        )
        return await self.get_case(case_id)

    async def _settle_account(
        self, customer_id: str, outcome: str, actor: str, case_id: int
    ) -> None:
        if self.accounts is None or not self.accounts.exists(customer_id):
            return
        current = self.accounts.get_status(customer_id)
        reason = f"Vaka #{case_id} analist kararı: {outcome}"
        if outcome == "FRAUD" and current != "BLOKE":
            await self.accounts.set_by_analyst(customer_id, "BLOKE", actor=actor, reason=reason)
        elif outcome == "TEMIZ" and current == "INCELENIYOR":
            # INCELENIYOR -> AKTIF is not a block lift; BLOKE needs maker-checker.
            await self.accounts.set_by_analyst(customer_id, "AKTIF", actor=actor, reason=reason)

    async def customer_confirmation(
        self, transaction_id: str, *, knows_payee: bool, note: str = ""
    ) -> dict[str, Any]:
        """Simulated "Bu kişiyi tanıyor musunuz?" answer for a held APP payment."""
        async with self.db.transaction() as session:
            alert = (
                (
                    await session.execute(
                        select(Alert)
                        .where(Alert.transaction_id == transaction_id)
                        .order_by(Alert.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if alert is None or alert.case_id is None:
                raise CaseNotFoundError(transaction_id)
            case = await self._case(session, alert.case_id)
            session.add(
                self._event(
                    case.id,
                    "CUSTOMER_CONFIRMATION",
                    "müşteri",
                    transaction_id=transaction_id,
                    knows_payee=knows_payee,
                    note=note,
                )
            )
            if not knows_payee:
                # the customer does not know the beneficiary: strong fraud evidence
                case.priority = round(float(case.priority or 0.0) * 2 + 1.0, 2)
                if case.status == "YENI":
                    case.status = "INCELENIYOR"
            case.updated_at = utcnow()
            case_id = case.id
        message = (
            "Teşekkürler. İşlem, cooling-off süresi sonunda analist onayıyla serbest bırakılacak."
            if knows_payee
            else "İşlem durduruldu; dolandırıcılık ekibimiz sizinle iletişime geçecek. "
            "Kimseyle şifre/OTP paylaşmayın."
        )
        return {"case_id": case_id, "knows_payee": knows_payee, "message": message}

    async def save_summary(self, case_id: int, summary: dict[str, Any], actor: str) -> None:
        async with self.db.transaction() as session:
            case = await self._case(session, case_id)
            case.summary = summary
            case.updated_at = utcnow()
            session.add(self._event(case_id, "COPILOT_SUMMARY", actor))

    # --- ŞİB ----------------------------------------------------------------------------------
    async def save_sib_draft(
        self, case_id: int, draft: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        async with self.db.transaction() as session:
            case = await self._case(session, case_id)
            if case.sib_status in ("SIB_ONAYLANDI",):
                raise CaseError("onaylanmış ŞİB değiştirilemez")
            if case.sib_status == "SIB_ONAY_BEKLIYOR":
                # A6: the approver must sign off the text that is submitted
                raise CaseError(
                    "ŞİB onay beklerken taslak değiştirilemez — önce onay talebi reddedilmeli"
                )
            case.sib_draft = draft
            case.sib_status = "SIB_TASLAK"
            case.updated_at = utcnow()
            session.add(self._event(case_id, "SIB_DRAFT", actor, fields=sorted(draft)))
        return await self.get_case(case_id)

    # --- maker-checker ------------------------------------------------------------------------
    async def request_approval(
        self, kind: str, target_id: str, payload: dict[str, Any], actor: str, note: str = ""
    ) -> dict[str, Any]:
        if kind not in APPROVAL_KINDS:
            raise CaseError(f"geçersiz onay türü: {kind}")
        try:
            result = await self._insert_approval(kind, target_id, payload, actor, note)
        except IntegrityError:
            # lost the race to a concurrent request (partial unique index)
            raise CaseError("bu hedef için bekleyen bir onay talebi zaten var") from None
        await self._audit(
            "APPROVAL_REQUESTED",
            str(result["id"]),
            actor,
            f"Onay talebi: {kind} → {target_id}",
            kind=kind,
            target_id=target_id,
        )
        return result

    async def _insert_approval(
        self, kind: str, target_id: str, payload: dict[str, Any], actor: str, note: str
    ) -> dict[str, Any]:
        async with self.db.transaction() as session:
            pending = (
                await session.execute(
                    select(Approval.id).where(
                        Approval.kind == kind,
                        Approval.target_id == target_id,
                        Approval.status == "BEKLIYOR",
                    )
                )
            ).first()
            if pending is not None:
                raise CaseError("bu hedef için bekleyen bir onay talebi zaten var")
            if kind == "SIB":
                case = await self._case(session, int(target_id))
                if not case.sib_draft:
                    raise CaseError("önce ŞİB taslağı kaydedilmeli")
                if case.decision != "FRAUD":
                    raise CaseError("ŞİB yalnızca FRAUD kararıyla kapanan vakalar için gönderilir")
                case.sib_status = "SIB_ONAY_BEKLIYOR"
                # A6: pin the reviewed text; _approve_sib refuses a different draft
                digest = sib_draft_hash(case.sib_draft)
                payload = {**payload, "draft_sha256": digest}
                session.add(
                    self._event(case.id, "SIB_APPROVAL_REQUESTED", actor, draft_sha256=digest)
                )
            approval = Approval(
                kind=kind,
                target_id=target_id,
                payload=payload,
                status="BEKLIYOR",
                requested_by=actor,
                note=note,
                created_at=utcnow(),
            )
            session.add(approval)
            await session.flush()
            result = _approval_dict(approval)
        return result

    async def list_approvals(self, status: str | None = "BEKLIYOR") -> list[dict[str, Any]]:
        query = select(Approval).order_by(Approval.id.desc())
        if status:
            query = query.where(Approval.status == status)
        async with self.db.session() as session:
            return [_approval_dict(a) for a in (await session.execute(query)).scalars()]

    async def decide_approval(
        self,
        approval_id: int,
        *,
        approve: bool,
        actor: str,
        note: str = "",
        role: str | None = None,
        via: str = "jwt",
    ) -> dict[str, Any]:
        """Approve/reject a pending request.

        ``role``/``via`` come from the authenticated principal (the API always
        passes them): the break-glass admin token never decides, and each kind
        has a minimum approver role. The status flip is a conditional UPDATE
        (``WHERE status='BEKLIYOR'``) so two concurrent approvers cannot both
        win.

        A5: the status flip, the handler's writes (rule / ``runtime_config``
        publish / account status / ŞİB), and every audit entry are one
        transaction; in-memory effects run only after the commit. A failing
        handler therefore leaves no partial effect and the request stays
        ``BEKLIYOR`` (retryable, or rejectable to free the pending slot).
        A concurrent config change maps to :class:`CaseError` (409), a
        database outage to :class:`ApprovalUnavailableError` (503).
        """
        if via == "admin_token":
            raise MakerCheckerError(
                "break-glass yönetici token'ı onay veremez — kişisel hesapla giriş yapın"
            )
        if self.writer is not None:
            await self.writer.flush()  # keep chain order with queued writes
        tx: ApprovalTx | None = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                if self.writer is not None:
                    await stack.enter_async_context(self.writer.chain.lock)
                session = await stack.enter_async_context(self.db.transaction())
                tx = ApprovalTx(session)
                snapshot, outcome = await self._decide_in(
                    tx, approval_id, approve=approve, actor=actor, note=note, role=role
                )
                if self.writer is not None:
                    await self.writer.chain.append_many(session, tx.audits)
        except BaseException as exc:
            for undo in reversed(tx.after_rollback if tx is not None else []):
                try:
                    undo()
                except Exception:
                    logger.exception("[Cases] onay #%s geri alma adımı başarısız", approval_id)
            if isinstance(exc, ConfigConflict):
                raise CaseError(
                    "yapılandırma eşzamanlı değişti — onay uygulanmadı, tekrar deneyin"
                ) from None
            if isinstance(exc, IntegrityError):
                raise CaseError("onay uygulanamadı: veri çakışması — tekrar deneyin") from None
            if isinstance(exc, SQLAlchemyError | ConnectionError | TimeoutError):
                logger.warning("[Cases] onay #%s uygulanamadı: %s", approval_id, type(exc).__name__)
                raise ApprovalUnavailableError(
                    "veritabanı şu an kullanılamıyor — onay uygulanmadı, tekrar deneyin"
                ) from exc
            raise
        for effect in tx.after_commit:
            try:
                pending = effect()
                if pending is not None:
                    await pending
            except Exception:  # committed + published: the config poller converges
                logger.exception("[Cases] onay #%s sonrası yerel uygulama başarısız", approval_id)
        return {**snapshot, "result": outcome}

    async def _decide_in(
        self,
        tx: ApprovalTx,
        approval_id: int,
        *,
        approve: bool,
        actor: str,
        note: str,
        role: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        session = tx.session
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise CaseNotFoundError(approval_id)
        if approval.status != "BEKLIYOR":
            raise CaseError("onay talebi zaten sonuçlandı")
        if approval.requested_by.casefold() == actor.casefold():
            raise MakerCheckerError("dört göz ilkesi: talebi açan kişi onaylayamaz")
        minimum = APPROVAL_MIN_ROLE.get(approval.kind, "admin")
        if role is not None and ROLE_RANK.get(role, -1) < ROLE_RANK[minimum]:
            raise MakerCheckerError(f"{approval.kind} onayı için en az '{minimum}' rolü gerekir")
        decided_at = utcnow()
        merged_note = (approval.note + "\n" + note).strip() if note else approval.note
        snapshot = {
            **_approval_dict(approval),
            "status": "ONAYLANDI" if approve else "REDDEDILDI",
            "decided_by": actor,
            "decided_at": decided_at,
            "note": merged_note,
        }
        claimed = await session.execute(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "BEKLIYOR")
            .values(
                status=snapshot["status"],
                decided_by=actor,
                decided_at=decided_at,
                note=merged_note,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(claimed, "rowcount", 0) != 1:
            raise CaseError("onay talebi zaten sonuçlandı")
        outcome: dict[str, Any] = {}
        if not approve and approval.kind == "SIB":
            case = await self._case(session, int(approval.target_id))
            case.sib_status = "SIB_TASLAK"
            session.add(self._event(case.id, "SIB_REJECTED", actor, note=note))
        if approve:
            handler = self.handlers.get(snapshot["kind"])
            if handler is not None:
                outcome = await handler(snapshot, actor, tx)
        tx.audits.append(
            AuditEntry(
                event_type="APPROVAL_DECIDED",
                entity_type="approval",
                entity_id=str(approval_id),
                actor=actor,
                reason=f"Onay {'verildi' if approve else 'reddedildi'}: {snapshot['kind']} → "
                f"{snapshot['target_id']}",
                payload={"kind": snapshot["kind"], "approved": approve},
            )
        )
        return snapshot, outcome

    async def _approve_unblock(
        self, approval: dict[str, Any], actor: str, tx: ApprovalTx
    ) -> dict[str, Any]:
        if self.accounts is None:
            raise CaseError("hesap servisi yok")
        accounts = self.accounts
        target = str(approval["payload"].get("to_status") or "AKTIF")
        customer_id = approval["target_id"]
        try:
            result, audit = await accounts.transition_in(
                tx.session,
                customer_id,
                target,
                actor=actor,
                reason=f"Maker-checker onayı #{approval['id']} (talep: {approval['requested_by']})",
            )
        except AccountNotFoundError:
            raise CaseError("müşteri hesabı bulunamadı ya da eşzamanlı değişti") from None
        tx.audits.append(audit)
        tx.on_commit(lambda: accounts.committed(result))
        return {"customer_id": customer_id, "hesap_durumu": target}

    async def _approve_sib(
        self, approval: dict[str, Any], actor: str, tx: ApprovalTx
    ) -> dict[str, Any]:
        case_id = int(approval["target_id"])
        reference = f"SIB-{utcnow():%Y%m%d}-{secrets.token_hex(3).upper()}"
        case = await self._case(tx.session, case_id)
        expected = (approval.get("payload") or {}).get("draft_sha256")
        if not expected or expected != sib_draft_hash(case.sib_draft):
            # A6: the draft changed after the request (or a legacy request
            # without a hash): reject it and request approval again
            raise CaseError(
                "ŞİB taslağı onay talebinden sonra değişti — talebi reddedip yeniden oluşturun"
            )
        case.sib_status = "SIB_ONAYLANDI"
        case.status = "SIB_GONDERILDI"
        case.updated_at = utcnow()
        draft = dict(case.sib_draft or {})
        draft["masak_reference"] = reference
        draft["submitted_at"] = utcnow().isoformat()
        case.sib_draft = draft
        tx.session.add(self._event(case_id, "SIB_SUBMITTED", actor, reference=reference))
        tx.on_commit(metrics.SIB_SUBMITTED.inc)
        return {"case_id": case_id, "masak_reference": reference}

    # --- SLA monitor (worker job) --------------------------------------------------------------
    async def sla_scan(self) -> dict[str, int]:
        now = utcnow()
        warn_days = get_settings().masak_warning_business_days
        async with self.db.session() as session:
            cases = (
                (await session.execute(select(Case).where(Case.status.in_(OPEN_STATUSES))))
                .scalars()
                .all()
            )
        breached = sum(
            1
            for c in cases
            if c.internal_sla_due and c.status == "YENI" and c.internal_sla_due < now
        )
        # L5: a passed MASAK deadline is "overdue", not "due soon"
        masak_overdue = sum(1 for c in cases if c.masak_deadline and c.masak_deadline < now)
        masak_soon = sum(
            1
            for c in cases
            if c.masak_deadline
            and c.masak_deadline >= now
            and sla.business_days_between(now, c.masak_deadline) <= warn_days
        )
        metrics.CASES_OPEN.set(len(cases))
        metrics.SLA_BREACHED.set(breached)
        metrics.MASAK_DUE_SOON.set(masak_soon)
        metrics.MASAK_OVERDUE.set(masak_overdue)
        return {
            "open": len(cases),
            "internal_sla_breached": breached,
            "masak_due_soon": masak_soon,
            "masak_overdue": masak_overdue,
        }


def _tx_dict(tx: Transaction | None) -> dict[str, Any] | None:
    if tx is None:
        return None
    return {
        "id": tx.id,
        "ts": tx.ts,
        "amount": float(tx.amount),
        "currency": tx.currency,
        "amount_try": float(tx.amount_try),
        "channel": tx.channel,
        "device_id": tx.device_id,
        "ip_address": tx.ip_address,
        "location": tx.location,
        "country": tx.country,
        "beneficiary_id": tx.beneficiary_id,
        "beneficiary_iban": tx.beneficiary_iban,
        "beneficiary_name": tx.beneficiary_name,
        "purpose": tx.purpose,
        "session": tx.session,
    }


def _decision_dict(d: Decision | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "decision": d.decision,
        "risk_score": d.risk_score,
        "components": d.components,
        "reason_codes": d.reason_codes,
        "rule_version": d.rule_version,
        "model_version": d.model_version,
        "latency_ms": d.latency_ms,
        "status": d.status,
        "hold_until": d.hold_until,
        "features": d.features,
    }


def _approval_dict(a: Approval) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "target_id": a.target_id,
        "payload": a.payload,
        "status": a.status,
        "requested_by": a.requested_by,
        "decided_by": a.decided_by,
        "decided_at": a.decided_at,
        "note": a.note,
        "created_at": a.created_at,
    }
