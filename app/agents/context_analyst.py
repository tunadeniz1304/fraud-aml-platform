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
from app.core.aggregates import MicroclusterDetector, OnlineStats
from app.core.behavior_store import BehaviorStore
from app.core.event_bus import EventBus
from app.core.mule_graph import DeviceMuleGraph, mule_explain
from app.core.risk_engine import _country_score, compute_risk, explain_factors
from app.core.sanctions import SanctionScreener
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
        self._records = self._load_records(customers_path or config.DATA_DIR / "customers.json")
        self.customers = {r["customer_id"]: r for r in self._records}
        self._recent: dict[str, list[datetime]] = {}
        self.analyzed: list[dict[str, Any]] = []
        self.llm_verdicts: list[dict[str, Any]] = []
        self.vector = behavior_store
        self.llm = llm
        self.microcluster = MicroclusterDetector()
        self.mule_graph = DeviceMuleGraph()
        self.sanctions = SanctionScreener().load()
        self.stats = OnlineStats()
        self.bus.subscribe(self.MONITORED, self._on_monitored)

    @staticmethod
    def _load_records(path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as fh:
            records = json.load(fh)
        return records if isinstance(records, list) else [records]

    @staticmethod
    def _load_customers(path: Path) -> dict[str, dict[str, Any]]:
        with path.open("r", encoding="utf-8") as fh:
            records = json.load(fh)
        return {record["customer_id"]: record for record in records}

    @staticmethod
    def _peer_avg(records: list[dict[str, Any]], record: dict[str, Any]) -> float:
        """Average amount of other customers from the same home city (peer group)."""
        city = record.get("home_city", "")
        peers = [
            r["avg_amount"]
            for r in records
            if r.get("home_city") == city and r["customer_id"] != record["customer_id"]
        ]
        return float(sum(peers) / len(peers)) if peers else float(record["avg_amount"])

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

        micro = self.microcluster.update(tx, ts.timestamp() * 1000)
        mule_ts = ts.timestamp() * 1000
        mule_scores = self.mule_graph.update(tx, mule_ts)
        mule = min(
            1.0,
            0.5 * mule_scores["shared_device"]
            + 0.3 * mule_scores["shared_beneficiary"]
            + 0.2 * mule_scores["fanout"],
        )

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
        final = round(
            min(1.0, self._merge_semantic(base, distance) + micro * 0.25 + mule * 0.2), 4
        )
        self.stats.update(final)

        factors_dict = {
            "amount": round(factors.amount, 3),
            "device": round(factors.device, 3),
            "location": round(factors.location, 3),
            "time": round(factors.time, 3),
            "velocity": round(factors.velocity, 3),
        }
        peer_avg = self._peer_avg(self._records, profile)
        # AML screening: customer name (and beneficiary, when present) against
        # the OFAC-style sanction/PEP list.
        screen_names = [profile.get("name", "")]
        if tx.get("beneficiary_id"):
            screen_names.append(tx["beneficiary_id"])
        sanction_hits: list[dict[str, Any]] = []
        for name in screen_names:
            for hit in self.sanctions.find_candidates(name):
                hit_copy = dict(hit)
                hit_copy["matched_on"] = name
                if hit_copy not in sanction_hits:
                    sanction_hits.append(hit_copy)
        analyzed = {
            **tx,
            "risk_score": final,
            "risk_score_rule": base,
            "semantic_distance": round(distance, 4) if distance is not None else None,
            "risk_factors": factors_dict,
            "risk_explanation": explain_factors(factors),
            "high_risk_country": _country_score(tx.get("country", "")) == 1.0,
            "peer_group_avg": round(peer_avg, 2),
            "peer_amount_ratio": (
                round(float(tx["amount"]) / peer_avg, 3) if peer_avg > 0 else None
            ),
            "microcluster": round(micro, 3),
            "mule_score": round(mule, 3),
            "mule_signals": mule_explain(mule_scores),
            "mule_scores": mule_scores,
            "sanctions": sanction_hits,
            "sanctions_hit": bool(sanction_hits),
        }

        verdict = None
        if self.llm is not None and self.llm.enabled:
            try:
                verdict = self.llm.analyze(
                    transaction=tx,
                    profile=profile,
                    risk_factors=factors_dict,
                    semantic_distance=distance,
                )
            except Exception as exc:
                # A provider outage / malformed reply must degrade to a
                # rule-only verdict, never kill the event stream.
                logger.error(
                    "[Analyst][LLM] %s analizi başarısız oldu (%s) — kural motoru kullanıldı",
                    tx["transaction_id"], exc,
                )
                analyzed["llm_error"] = str(exc)
                verdict = None
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
