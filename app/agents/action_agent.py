"""ActionAgent agent.

Final stage of the pipeline. Maps each analyzed transfer to a decision and
applies it through the account **state machine** (bug #4): system decisions
may only escalate ``AKTIF → INCELENIYOR → BLOKE`` and never downgrade a blocked
account. Every decision appends exactly one audit row.

* ``risk >= risk_threshold``            → ``BLOKE`` (account blocked, event emitted)
* ``risk >= warning_threshold`` or a forced review (sanctions hit, unknown
  customer)                              → ``INCELENIYOR``
* otherwise                              → ``GECTI``
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.core.account_store import AccountNotFoundError, AccountStore
from app.core.event_bus import EventBus

logger = logging.getLogger("fraud.action")


class ActionAgent:
    """Third stage: autonomous account actions + audit persistence."""

    ANALYZED = "transaction.analyzed"
    BLOCKED = "account.blocked"
    DECIDED = "decision.made"

    def __init__(
        self, bus: EventBus, store: AccountStore | None = None, buffer: int | None = None
    ) -> None:
        size = buffer or get_settings().analyzed_buffer
        self.bus = bus
        self.store = store
        self.blocked: deque[dict[str, Any]] = deque(maxlen=size)
        self.passed: deque[dict[str, Any]] = deque(maxlen=size)
        self.warned: deque[dict[str, Any]] = deque(maxlen=size)
        self.bus.subscribe(self.ANALYZED, self._on_analyzed)

    async def _on_analyzed(self, tx: dict[str, Any]) -> None:
        settings = get_settings()
        risk = float(tx.get("risk_score", 0.0))
        if tx.get("account_blocked") or risk >= settings.risk_threshold:
            decision = "BLOKE"
            block = self._block(tx, risk)
            await self.bus.publish(self.BLOCKED, block)
        elif risk >= settings.warning_threshold or tx.get("force_review"):
            decision = "INCELENIYOR"
            self._warn(tx, risk)
        else:
            decision = "GECTI"
            self._pass(tx, risk)
        await self.bus.publish(self.DECIDED, {**tx, "decision_legacy": decision})

    # --- persistence helpers ---------------------------------------------------
    def _apply(self, tx: dict[str, Any], requested: str | None, decision: str, reason: str) -> None:
        if self.store is None:
            return
        customer_id = tx["customer_id"]
        if requested is not None:
            try:
                previous, new = self.store.escalate(customer_id, requested)
                if previous != new:
                    logger.warning("[Action] %s hesap durumu %s → %s", customer_id, previous, new)
            except AccountNotFoundError:
                logger.warning(
                    "[Action] %s için hesap kaydı yok — durum güncellenmedi (işlem %s)",
                    customer_id,
                    tx["transaction_id"],
                )
            except Exception:
                logger.exception("[Action] hesap güncelleme hatası")
        try:
            self.store.append_audit(
                transaction_id=tx["transaction_id"],
                customer_id=customer_id,
                risk_score=float(tx.get("risk_score", 0.0)),
                decision=decision,
                reason=reason,
            )
        except Exception:  # persistence must not kill the pipeline
            logger.exception("[Action] audit kaydı hatası")

    # --- branches --------------------------------------------------------------
    def _block(self, tx: dict[str, Any], risk: float) -> dict[str, Any]:
        settings = get_settings()
        if tx.get("account_blocked"):
            reason = "Hesap zaten BLOKE: işlem skorlanmadan reddedildi"
        else:
            reason = (
                f"Otonom bloke: risk skoru eşiği aştı ({risk:.2f} >= {settings.risk_threshold})"
            )
        block = {
            "transaction_id": tx["transaction_id"],
            "customer_id": tx["customer_id"],
            "risk_score": risk,
            "blocked_at": datetime.now(UTC).isoformat(),
            "reason": reason,
        }
        self.blocked.append(block)
        logger.warning(
            "[Action] OTONOM HESAP BLOKE — %s (müşteri %s, risk %.2f)",
            tx["transaction_id"],
            tx["customer_id"],
            risk,
        )
        self._apply(tx, "BLOKE", "BLOKE", reason)
        return block

    def _warn(self, tx: dict[str, Any], risk: float) -> None:
        reasons = []
        if tx.get("sanctions_hit"):
            reasons.append("yaptırım/PEP listesi eşleşmesi (zorunlu inceleme)")
        if tx.get("unknown_customer"):
            reasons.append("bilinmeyen müşteri")
        if not reasons:
            reasons.append("uyarı eşiği aşıldı")
        reason = "İnsan incelemesi gerekli: " + ", ".join(reasons)
        self.warned.append(
            {
                "transaction_id": tx["transaction_id"],
                "customer_id": tx["customer_id"],
                "risk_score": risk,
                "reason": reason,
            }
        )
        logger.warning("[Action] %s incelemeye alındı (risk %.2f)", tx["transaction_id"], risk)
        self._apply(tx, "INCELENIYOR", "INCELENIYOR", reason)

    def _pass(self, tx: dict[str, Any], risk: float) -> None:
        self.passed.append(
            {
                "transaction_id": tx["transaction_id"],
                "customer_id": tx["customer_id"],
                "risk_score": risk,
            }
        )
        logger.info("[Action] %s normal akışta (risk %.2f)", tx["transaction_id"], risk)
        self._apply(tx, None, "GECTI", "Risk eşiği aşılmadı")
