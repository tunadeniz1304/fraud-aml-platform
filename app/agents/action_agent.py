"""ActionAgent agent.

Final stage of the pipeline. Applies the policy decision of each analyzed
transfer through the account **state machine** (bug #4: system decisions only
escalate ``AKTIF → INCELENIYOR → BLOKE``) and persists the transaction, the
decision (components, reason codes, rule/model versions, latency) and a
hash-chained audit entry through the write-behind writer.

=========  =========================  ===================  ==============
decision   account                    legacy list          legacy label
=========  =========================  ===================  ==============
BLOCK      → BLOKE                    ``blocked``          BLOKE
HOLD       → INCELENIYOR (cooling-off) ``warned``           INCELENIYOR
STEP_UP    unchanged (OTP simulated)  ``warned``           GECTI
ALLOW      unchanged                  ``passed``           GECTI
=========  =========================  ===================  ==============

Events produced by legacy publishers that only carry ``risk_score`` are
mapped with :meth:`PolicyEngine.decide_from_risk`.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.core.event_bus import EventBus
from app.db import repository as repo
from app.db.audit import AuditEntry
from app.db.writer import PersistenceWriter
from app.monitoring import metrics
from app.scoring.policy import ACCOUNT_TARGET, LEGACY, PolicyEngine
from app.services.accounts import AccountNotFoundError, AccountService

logger = logging.getLogger("fraud.action")

_TEXT = {
    "BLOCK": "Otonom bloke",
    "HOLD": "İşlem bekletmeye alındı, analist onayı gerekli",
    "STEP_UP": "Ek doğrulama (OTP/biyometrik) istendi",
    "ALLOW": "Risk eşiği aşılmadı",
}


class ActionAgent:
    """Third stage: graduated actions + persistence."""

    ANALYZED = "transaction.analyzed"
    BLOCKED = "account.blocked"
    DECIDED = "decision.made"

    def __init__(
        self,
        bus: EventBus,
        accounts: AccountService | None = None,
        writer: PersistenceWriter | None = None,
        buffer: int | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        size = buffer or get_settings().analyzed_buffer
        self.bus = bus
        self.accounts = accounts
        self.writer = writer
        self.policy = policy or PolicyEngine()
        self.blocked: deque[dict[str, Any]] = deque(maxlen=size)
        self.passed: deque[dict[str, Any]] = deque(maxlen=size)
        self.warned: deque[dict[str, Any]] = deque(maxlen=size)
        self.bus.subscribe(self.ANALYZED, self._on_analyzed)

    def _resolve(self, tx: dict[str, Any]) -> tuple[str, float]:
        risk = float(tx.get("risk_score", 0.0))
        decision = tx.get("decision")
        if decision in LEGACY:
            return str(decision), risk
        fallback = self.policy.decide_from_risk(
            risk,
            force_review=bool(tx.get("force_review")),
            account_blocked=bool(tx.get("account_blocked")),
        )
        tx.setdefault("case_required", fallback.case_required)
        tx.setdefault("hold_minutes", fallback.hold_minutes)
        return fallback.decision, risk

    @staticmethod
    def _reason(tx: dict[str, Any], decision: str, risk: float) -> str:
        if tx.get("account_blocked"):
            return "Hesap zaten BLOKE: işlem skorlanmadan reddedildi"
        extras: list[str] = []
        if tx.get("sanctions_hit"):
            extras.append("yaptırım/PEP listesi eşleşmesi (zorunlu inceleme)")
        if tx.get("unknown_customer"):
            extras.append("bilinmeyen müşteri")
        top = [
            str(r.get("text")) for r in tx.get("reason_codes") or [] if r.get("source") != "policy"
        ]
        extras.extend(top[:2])
        head = f"{_TEXT[decision]} (risk {risk:.2f})"
        return head + (": " + "; ".join(extras) if extras else "")

    async def _on_analyzed(self, tx: dict[str, Any]) -> None:
        decision, risk = self._resolve(tx)
        reason = self._reason(tx, decision, risk)
        entry = {
            "transaction_id": tx["transaction_id"],
            "customer_id": tx["customer_id"],
            "risk_score": risk,
            "decision": decision,
            "reason": reason,
        }
        if decision == "BLOCK":
            self.blocked.append({**entry, "blocked_at": datetime.now(UTC).isoformat()})
            logger.warning(
                "[Action] OTONOM HESAP BLOKE — %s (müşteri %s, risk %.2f)",
                tx["transaction_id"],
                tx["customer_id"],
                risk,
            )
        elif decision in ("HOLD", "STEP_UP"):
            self.warned.append(entry)
            logger.warning("[Action] %s → %s (risk %.2f)", tx["transaction_id"], decision, risk)
        else:
            self.passed.append(entry)
            logger.debug("[Action] %s normal akışta (risk %.2f)", tx["transaction_id"], risk)
        metrics.DECISIONS.labels(decision=decision).inc()
        metrics.RISK_SCORE.observe(risk)

        await self._apply_status(tx, decision, reason, risk)
        await self._persist(tx, decision, reason, risk)
        if decision == "BLOCK":
            await self.bus.publish(self.BLOCKED, self.blocked[-1], key=str(tx["transaction_id"]))
        await self.bus.publish(
            self.DECIDED,
            {
                **tx,
                "decision": decision,
                "decision_legacy": LEGACY[decision],
                "decision_reason": reason,
            },
            key=str(tx["transaction_id"]),
        )

    async def _apply_status(
        self, tx: dict[str, Any], decision: str, reason: str, risk: float
    ) -> None:
        target = ACCOUNT_TARGET.get(decision)
        if self.accounts is None or target is None:
            return
        try:
            await self.accounts.escalate(
                tx["customer_id"],
                target,
                reason=reason,
                transaction_id=tx["transaction_id"],
                risk_score=risk,
            )
        except AccountNotFoundError:
            logger.warning(
                "[Action] %s için hesap kaydı yok — durum güncellenmedi (işlem %s)",
                tx["customer_id"],
                tx["transaction_id"],
            )

    async def _persist(self, tx: dict[str, Any], decision: str, reason: str, risk: float) -> None:
        if self.writer is None:
            return
        hold_minutes = tx.get("hold_minutes")
        hold_until = (
            datetime.now(UTC) + timedelta(minutes=int(hold_minutes))
            if decision == "HOLD" and hold_minutes
            else None
        )
        try:
            await self.writer.record_decision(
                repo.transaction_values(tx),
                {
                    "transaction_id": str(tx["transaction_id"]),
                    "customer_id": str(tx["customer_id"]),
                    "decision": decision,
                    "risk_score": risk,
                    "components": {
                        **(tx.get("components") or {}),
                        "shap": tx.get("explanation") or [],
                    },
                    "reason_codes": tx.get("reason_codes") or [],
                    "features": tx.get("features") or {},
                    "rule_version": str(tx.get("rule_version") or ""),
                    "model_version": str(tx.get("model_version") or ""),
                    "challenger_version": tx.get("challenger_version"),
                    "challenger_score": tx.get("challenger_score"),
                    "latency_ms": float(tx.get("latency_ms") or 0.0),
                    "status": "BEKLEMEDE" if decision == "HOLD" else "FINAL",
                    "hold_until": hold_until,
                },
                AuditEntry(
                    event_type="DECISION",
                    transaction_id=str(tx["transaction_id"]),
                    customer_id=str(tx["customer_id"]),
                    entity_id=str(tx["transaction_id"]),
                    risk_score=risk,
                    decision=LEGACY[decision],
                    reason=reason,
                    payload={
                        "policy_decision": decision,
                        "rule_version": tx.get("rule_version") or "",
                        "model_version": tx.get("model_version") or "",
                        "reason_codes": [r.get("code") for r in tx.get("reason_codes") or []],
                    },
                ),
            )
        except Exception:
            logger.exception("[Action] kalıcılık hatası (%s)", tx["transaction_id"])
