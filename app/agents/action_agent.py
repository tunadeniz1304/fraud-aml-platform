"""ActionAgent agent.

Final stage of the pipeline. Maps each analyzed transfer to a decision,
applies it through the account **state machine** (bug #4: system decisions
only escalate ``AKTIF → INCELENIYOR → BLOKE``) and persists the transaction,
the decision and a hash-chained audit entry through the write-behind writer.

* ``risk >= risk_threshold`` or an already blocked account → ``BLOKE``
* ``risk >= warning_threshold`` or a forced review (sanctions hit, unknown
  customer)                                                 → ``INCELENIYOR``
* otherwise                                                 → ``GECTI``
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.core.decisions import legacy_decision
from app.core.event_bus import EventBus
from app.db import repository as repo
from app.db.audit import AuditEntry
from app.db.writer import PersistenceWriter
from app.monitoring import metrics
from app.services.accounts import AccountNotFoundError, AccountService

logger = logging.getLogger("fraud.action")

_TARGET = {"BLOKE": "BLOKE", "INCELENIYOR": "INCELENIYOR"}


class ActionAgent:
    """Third stage: autonomous account actions + persistence."""

    ANALYZED = "transaction.analyzed"
    BLOCKED = "account.blocked"
    DECIDED = "decision.made"

    def __init__(
        self,
        bus: EventBus,
        accounts: AccountService | None = None,
        writer: PersistenceWriter | None = None,
        buffer: int | None = None,
    ) -> None:
        size = buffer or get_settings().analyzed_buffer
        self.bus = bus
        self.accounts = accounts
        self.writer = writer
        self.blocked: deque[dict[str, Any]] = deque(maxlen=size)
        self.passed: deque[dict[str, Any]] = deque(maxlen=size)
        self.warned: deque[dict[str, Any]] = deque(maxlen=size)
        self.bus.subscribe(self.ANALYZED, self._on_analyzed)

    @staticmethod
    def _reason(tx: dict[str, Any], decision: str, risk: float) -> str:
        settings = get_settings()
        if decision == "BLOKE":
            if tx.get("account_blocked"):
                return "Hesap zaten BLOKE: işlem skorlanmadan reddedildi"
            return f"Otonom bloke: risk skoru eşiği aştı ({risk:.2f} >= {settings.risk_threshold})"
        if decision == "INCELENIYOR":
            reasons = []
            if tx.get("sanctions_hit"):
                reasons.append("yaptırım/PEP listesi eşleşmesi (zorunlu inceleme)")
            if tx.get("unknown_customer"):
                reasons.append("bilinmeyen müşteri")
            return "İnsan incelemesi gerekli: " + (", ".join(reasons) or "uyarı eşiği aşıldı")
        return "Risk eşiği aşılmadı"

    async def _on_analyzed(self, tx: dict[str, Any]) -> None:
        risk = float(tx.get("risk_score", 0.0))
        decision = legacy_decision(
            risk,
            force_review=bool(tx.get("force_review")),
            account_blocked=bool(tx.get("account_blocked")),
        )
        reason = self._reason(tx, decision, risk)
        entry = {
            "transaction_id": tx["transaction_id"],
            "customer_id": tx["customer_id"],
            "risk_score": risk,
            "reason": reason,
        }
        if decision == "BLOKE":
            block = {**entry, "blocked_at": datetime.now(UTC).isoformat()}
            self.blocked.append(block)
            logger.warning(
                "[Action] OTONOM HESAP BLOKE — %s (müşteri %s, risk %.2f)",
                tx["transaction_id"],
                tx["customer_id"],
                risk,
            )
        elif decision == "INCELENIYOR":
            self.warned.append(entry)
            logger.warning("[Action] %s incelemeye alındı (risk %.2f)", tx["transaction_id"], risk)
        else:
            self.passed.append(entry)
            logger.info("[Action] %s normal akışta (risk %.2f)", tx["transaction_id"], risk)
        metrics.DECISIONS.labels(decision=decision).inc()
        metrics.RISK_SCORE.observe(risk)

        await self._apply_status(tx, decision, reason, risk)
        await self._persist(tx, decision, reason, risk)
        if decision == "BLOKE":
            await self.bus.publish(self.BLOCKED, self.blocked[-1], key=str(tx["transaction_id"]))
        await self.bus.publish(
            self.DECIDED,
            {**tx, "decision_legacy": decision, "decision_reason": reason},
            key=str(tx["transaction_id"]),
        )

    async def _apply_status(
        self, tx: dict[str, Any], decision: str, reason: str, risk: float
    ) -> None:
        target = _TARGET.get(decision)
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
        try:
            await self.writer.record_decision(
                repo.transaction_values(tx),
                {
                    "transaction_id": str(tx["transaction_id"]),
                    "customer_id": str(tx["customer_id"]),
                    "decision": decision,
                    "risk_score": risk,
                    "components": {
                        "rule": tx.get("risk_score_rule"),
                        "microcluster": tx.get("microcluster"),
                        "mule": tx.get("mule_score"),
                    },
                    "reason_codes": [
                        {"code": "LEGACY", "text": t} for t in tx.get("risk_explanation") or []
                    ],
                    "features": tx.get("features") or {},
                    "rule_version": "v1",
                    "model_version": "",
                    "latency_ms": float(tx.get("latency_ms") or 0.0),
                    "status": "FINAL",
                },
                AuditEntry(
                    event_type="DECISION",
                    transaction_id=str(tx["transaction_id"]),
                    customer_id=str(tx["customer_id"]),
                    entity_id=str(tx["transaction_id"]),
                    risk_score=risk,
                    decision=decision,
                    reason=reason,
                ),
            )
        except Exception:
            logger.exception("[Action] kalıcılık hatası (%s)", tx["transaction_id"])
