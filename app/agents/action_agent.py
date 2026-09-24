"""ActionAgent agent.

Final stage of the pipeline. Evaluates each analyzed transfer and, whenever the
risk score meets or exceeds the configured threshold, *autonomously* places a
temporary block on the customer's account: updates the ``hesap_durumu`` column
to ``BLOKE`` in the relational store, appends an audit-log row, and emits an
``account.blocked`` event. Scores below the threshold but at/above the warning
threshold are flagged as ``INCELENIYOR`` for human review.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app import config
from app.core.account_store import AccountStore
from app.core.event_bus import EventBus

logger = logging.getLogger("fraud.action")


class ActionAgent:
    """Third stage: autonomous account blocking + audit persistence."""

    ANALYZED = "transaction.analyzed"
    BLOCKED = "account.blocked"

    def __init__(self, bus: EventBus, store: AccountStore | None = None) -> None:
        self.bus = bus
        self.store = store or AccountStore()
        self.blocked: list[dict[str, Any]] = []
        self.passed: list[dict[str, Any]] = []
        self.warned: list[dict[str, Any]] = []
        self.bus.subscribe(self.ANALYZED, self._on_analyzed)

    async def _on_analyzed(self, tx: dict[str, Any]) -> None:
        risk = float(tx.get("risk_score", 0.0))
        tx["transaction_id"]
        tx["customer_id"]

        if risk >= config.RISK_THRESHOLD:
            await self.bus.publish(self.BLOCKED, self._block(tx, risk))
        elif risk >= config.WARNING_THRESHOLD:
            self._warn(tx, risk)
        else:
            self._pass(tx, risk)

    # --- branches ----------------------------------------------------------
    def _block(self, tx: dict[str, Any], risk: float) -> dict[str, Any]:
        block = {
            "transaction_id": tx["transaction_id"],
            "customer_id": tx["customer_id"],
            "risk_score": risk,
            "blocked_at": datetime.now(UTC).isoformat(),
            "reason": (
                f"Otonom bloke: risk skoru eşiği aştı ({risk:.2f} >= {config.RISK_THRESHOLD})"
            ),
        }
        self.blocked.append(block)
        logger.warning(
            "[Action] OTONOM HESAP BLOKE — %s (müşteri %s, risk %.2f): "
            "hesabın durumu BLOKE yapıldı, inceleme kuyruğuna alındı",
            tx["transaction_id"],
            tx["customer_id"],
            risk,
        )
        if self.store:
            try:
                self.store.set_hesap_durumu(tx["customer_id"], "BLOKE")
                self.store.append_audit(
                    transaction_id=tx["transaction_id"],
                    customer_id=tx["customer_id"],
                    risk_score=risk,
                    decision="BLOKE",
                    reason=block["reason"],
                )
            except Exception as exc:  # persistence must not kill the pipeline
                logger.error("[Action] hesap güncelleme hatası: %s", exc)
        return block

    def _warn(self, tx: dict[str, Any], risk: float) -> None:
        self.warned.append(
            {
                "transaction_id": tx["transaction_id"],
                "customer_id": tx["customer_id"],
                "risk_score": risk,
            }
        )
        logger.warning(
            "[Action] %s insan incelemesi için işaretlendi (risk %.2f >= %.2f)",
            tx["transaction_id"],
            risk,
            config.WARNING_THRESHOLD,
        )
        if self.store:
            try:
                self.store.set_hesap_durumu(tx["customer_id"], "INCELENIYOR")
                self.store.append_audit(
                    transaction_id=tx["transaction_id"],
                    customer_id=tx["customer_id"],
                    risk_score=risk,
                    decision="INCELENIYOR",
                    reason="İnsan incelemesi gerekli (uyarı eşiği aşıldı)",
                )
            except Exception as exc:
                logger.error("[Action] inceleme kaydı hatası: %s", exc)

    def _pass(self, tx: dict[str, Any], risk: float) -> None:
        self.passed.append(
            {
                "transaction_id": tx["transaction_id"],
                "customer_id": tx["customer_id"],
                "risk_score": risk,
            }
        )
        logger.info(
            "[Action] %s normal akışta (risk %.2f < eşik %.2f) — bloke yok",
            tx["transaction_id"],
            risk,
            config.WARNING_THRESHOLD,
        )
        if self.store:
            try:
                self.store.append_audit(
                    transaction_id=tx["transaction_id"],
                    customer_id=tx["customer_id"],
                    risk_score=risk,
                    decision="GECTI",
                    reason="Risk eşiği aşılmadı",
                )
            except Exception as exc:
                logger.error("[Action] audit kaydı hatası: %s", exc)
