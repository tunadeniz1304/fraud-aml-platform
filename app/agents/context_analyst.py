"""ContextAnalyst agent.

Consumes monitored transfers, looks up the customer's behavioural context,
scores the transfer 0..1 via the rule engine, compares it semantically against
the customer's embedded past behaviour (ChromaDB RAG) and, when a real LLM
provider is configured, asks the model for a narrative fraud verdict.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app import config
from app.core.behavior_store import BehaviorStore
from app.core.event_bus import EventBus
from app.core.risk_engine import compute_risk
from app.llm import LLMClient

logger = logging.getLogger("fraud.analyst")


class ContextAnalyst:
    """Second stage: behavioural + semantic + LLM analysis."""

    MONITORED = "transaction.monitored"
    ANALYZED = "transaction.analyzed"

    def __init__(
        self,
        bus: EventBus,
        customers_path: Path | None = None,
        behavior_store: BehaviorStore | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self.bus = bus
        self.customers = self._load_customers(customers_path or config.DATA_DIR / "customers.json")
        self._recent: dict[str, list[datetime]] = {}
        self.analyzed: list[dict[str, Any]] = []
        self.llm_verdicts: list[dict[str, Any]] = []
        self.vector = behavior_store
        self.llm = llm
        self.bus.subscribe(self.MONITORED, self._on_monitored)

    @staticmethod
    def _load_customers(path: Path) -> dict[str, dict[str, Any]]:
        with path.open("r", encoding="utf-8") as fh:
            records = json.load(fh)
        return {record["customer_id"]: record for record in records}

    def _velocity_last_hour(self, customer_id: str, ts: datetime) -> int:
        cutoff = ts - timedelta(hours=1)
        return sum(1 for t in self._recent.get(customer_id, []) if t >= cutoff)

    @staticmethod
    def _merge_semantic(score: float, distance: float | None) -> float:
        """Fold semantic distance into the rule score (0..~2 -> 0..1)."""
        if distance is None:
            return score
        normalised = max(0.0, min(1.0, distance / 2.0))
        return min(1.0, score + config.settings.semantic_cap * normalised)

    async def _on_monitored(self, tx: dict[str, Any]) -> None:
        customer_id = tx["customer_id"]
        profile = self.customers.get(customer_id)

        if profile is None:
            await self._publish_analyzed({**tx, "risk_score": 1.0, "risk_factors": None})
            return

        ts = datetime.fromisoformat(tx["ts"])
        velocity = self._velocity_last_hour(customer_id, ts)
        self._recent.setdefault(customer_id, []).append(ts)

        factors = compute_risk(
            amount=float(tx["amount"]),
            avg_amount=float(profile["avg_amount"]),
            device_id=tx["device_id"],
            known_device_ids=set(profile["known_device_ids"]),
            location=tx["location"],
            known_locations=set(profile["known_locations"]),
            hour=ts.hour,
            typical_hours=set(profile["typical_hours"]),
            events_last_hour=velocity,
        )
        distance = None
        if self.vector is not None:
            distance = self.vector.semantic_distance(tx)
        base = round(factors.score, 4)
        final = round(self._merge_semantic(base, distance), 4)

        factors_dict = {
            "amount": round(factors.amount, 3),
            "device": round(factors.device, 3),
            "location": round(factors.location, 3),
            "time": round(factors.time, 3),
            "velocity": round(factors.velocity, 3),
        }
        analyzed = {
            **tx,
            "risk_score": final,
            "risk_score_rule": base,
            "semantic_distance": round(distance, 4) if distance is not None else None,
            "risk_factors": factors_dict,
        }

        verdict = None
        if self.llm is not None and self.llm.enabled:
            verdict = self.llm.analyze(
                transaction=tx,
                profile=profile,
                risk_factors=factors_dict,
                semantic_distance=distance,
            )
            if verdict:
                analyzed["llm"] = verdict
                self.llm_verdicts.append(verdict)
                logger.info(
                    "[Analyst][LLM] %s -> fraud=%s tip=%s aksiyon=%s",
                    tx["transaction_id"],
                    verdict.get("is_fraud"),
                    verdict.get("fraud_type"),
                    verdict.get("recommended_action"),
                )

        self.analyzed.append(analyzed)
        logger.info(
            "[Analyst] %s risk skoru %.2f (kural %.2f, semantik %s)",
            tx["transaction_id"], final, base,
            f"{distance:.2f}" if distance is not None else "kapalı",
        )
        await self._publish_analyzed(analyzed)

    async def _publish_analyzed(self, analyzed: dict[str, Any]) -> None:
        await self.bus.publish(self.ANALYZED, analyzed)
