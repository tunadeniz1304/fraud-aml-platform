"""ContextAnalyst agent.

Consumes monitored transfers, builds point-in-time features from the
streaming feature store and scores each transfer 0..1. Guarantees:

* the synchronous path never calls an LLM (bug #9);
* amounts are normalised to TRY before any comparison (bug #12);
* sanctions, country, IP, channel and purpose signals feed the score and a
  sanctions hit forces human review (bug #6);
* transfers of an already ``BLOKE`` account are stopped *before* scoring
  (bug #5); unknown customers go to review instead of a silent ``1.0`` (bug #8);
* all buffers are bounded (bug #11) — windows live in the feature store;
* the adaptive profile only learns from transfers that pass (poisoning
  protection).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.aggregates import MicroclusterDetector, OnlineStats
from app.core.decisions import legacy_decision
from app.core.event_bus import EventBus
from app.core.mule_graph import DeviceMuleGraph, mule_explain
from app.core.risk_engine import _country_score, compute_risk, explain_factors
from app.core.sanctions import SanctionScreener
from app.features.extractor import CustomerDirectory, FeatureExtractor

logger = logging.getLogger("fraud.analyst")

StatusLookup = Callable[[str], str | None]


class ContextAnalyst:
    """Second stage: behavioural + context analysis (synchronous, no LLM)."""

    MONITORED = "transaction.monitored"
    ANALYZED = "transaction.analyzed"

    def __init__(
        self,
        bus: EventBus,
        customers_path: Path | None = None,
        *,
        account_status: StatusLookup | None = None,
        extractor: FeatureExtractor | None = None,
        buffer: int | None = None,
    ) -> None:
        settings = get_settings()
        self.bus = bus
        if extractor is None:
            directory = CustomerDirectory.from_file(
                customers_path or settings.resolved_customers_path
            )
            extractor = FeatureExtractor(customers=directory)
        self.extractor = extractor
        self.directory = extractor.customers
        self.customers = {r["customer_id"]: r for r in self.directory.records()}
        self._peer_cache = self._peer_averages(list(self.customers.values()))
        self.analyzed: deque[dict[str, Any]] = deque(maxlen=buffer or settings.analyzed_buffer)
        self.microcluster = MicroclusterDetector()
        self.mule_graph = DeviceMuleGraph()
        self.sanctions = SanctionScreener().load()
        self.stats = OnlineStats()
        self.account_status = account_status
        self.bus.subscribe(self.MONITORED, self._on_monitored)

    # --- helpers -------------------------------------------------------------
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

    @classmethod
    def _peer_averages(cls, records: list[dict[str, Any]]) -> dict[str, float]:
        by_city: dict[str, list[tuple[str, float]]] = {}
        for r in records:
            by_city.setdefault(r.get("home_city", ""), []).append(
                (r["customer_id"], float(r.get("avg_amount", 0)))
            )
        out: dict[str, float] = {}
        for members in by_city.values():
            total = sum(a for _, a in members)
            for cid, own in members:
                peers = len(members) - 1
                out[cid] = (total - own) / peers if peers else own
        return out

    def _screen(self, profile: dict[str, Any], tx: dict[str, Any]) -> list[dict[str, Any]]:
        """AML screening of customer and beneficiary names against the list."""
        names = [profile.get("name", "")]
        for key in ("beneficiary_name", "beneficiary_id"):
            if tx.get(key):
                names.append(str(tx[key]))
        hits: list[dict[str, Any]] = []
        for name in names:
            for hit in self.sanctions.find_candidates(name):
                hit_copy = {**hit, "matched_on": name}
                if hit_copy not in hits:
                    hits.append(hit_copy)
        return hits

    # --- event handler ---------------------------------------------------------
    async def _on_monitored(self, tx: dict[str, Any]) -> None:
        settings = get_settings()
        customer_id = tx["customer_id"]
        extraction = await self.extractor.extract(tx)
        amount_try = extraction.tx.amount_try

        status = self.account_status(customer_id) if self.account_status else None
        if status == "BLOKE":
            logger.warning(
                "[Analyst] %s: hesap %s zaten BLOKE — işlem skorlanmadan durduruldu",
                tx["transaction_id"],
                customer_id,
            )
            await self._publish(
                {
                    **tx,
                    "amount_try": amount_try,
                    "risk_score": 1.0,
                    "risk_factors": None,
                    "risk_explanation": ["hesap zaten bloke"],
                    "account_blocked": True,
                    "scored": False,
                }
            )
            return

        profile = self.customers.get(customer_id)
        if profile is None:
            logger.warning(
                "[Analyst] %s: bilinmeyen müşteri %s — skorlanamadı, incelemeye yönlendirildi",
                tx["transaction_id"],
                customer_id,
            )
            await self._publish(
                {
                    **tx,
                    "amount_try": amount_try,
                    "risk_score": settings.unknown_customer_risk,
                    "risk_factors": None,
                    "risk_explanation": ["bilinmeyen müşteri"],
                    "unknown_customer": True,
                    "force_review": True,
                    "scored": False,
                }
            )
            return

        features = extraction.features
        ts = extraction.tx.ts
        ts_ms = extraction.tx.epoch * 1000
        micro = self.microcluster.update(tx, ts_ms)
        mule_scores = self.mule_graph.update(tx, ts_ms)
        mule = min(
            1.0,
            0.5 * mule_scores["shared_device"]
            + 0.3 * mule_scores["shared_beneficiary"]
            + 0.2 * mule_scores["fanout"],
        )
        static = extraction.context.customer
        factors = compute_risk(
            amount=amount_try,
            avg_amount=static.avg_amount_try,
            device_id=tx["device_id"],
            known_device_ids=set(static.known_devices),
            location=tx["location"],
            known_locations=set(static.known_locations),
            hour=ts.hour,
            typical_hours=set(static.typical_hours),
            events_last_hour=int(features["cnt_1h"]),
            country=tx.get("country", ""),
            purpose=tx.get("purpose", ""),
            ip_address=tx.get("ip_address"),
            channel=tx.get("channel"),
            known_channels=set(static.known_channels) or None,
        )
        base = round(factors.score, 4)
        final = round(
            min(1.0, base + micro * settings.microcluster_weight + mule * settings.mule_weight), 4
        )
        self.stats.update(final)
        sanction_hits = self._screen(profile, tx)
        peer_avg = self._peer_cache.get(customer_id) or static.avg_amount_try
        decision = legacy_decision(final, force_review=bool(sanction_hits))
        await self.extractor.commit(extraction, learn_profile=decision == "GECTI")
        analyzed = {
            **tx,
            "amount_try": amount_try,
            "risk_score": final,
            "risk_score_rule": base,
            "risk_factors": factors.as_dict(),
            "risk_explanation": explain_factors(factors),
            "high_risk_country": _country_score(tx.get("country", "")) == 1.0,
            "peer_group_avg": round(peer_avg, 2),
            "peer_amount_ratio": round(amount_try / peer_avg, 3) if peer_avg > 0 else None,
            "microcluster": round(micro, 3),
            "mule_score": round(mule, 3),
            "mule_signals": mule_explain(mule_scores),
            "mule_scores": mule_scores,
            "sanctions": sanction_hits,
            "sanctions_hit": bool(sanction_hits),
            "force_review": bool(sanction_hits),
            "features": features,
            "scored": True,
        }
        logger.info(
            "[Analyst] %s risk skoru %.2f (kural %.2f, yaptırım=%s)",
            tx["transaction_id"],
            final,
            base,
            "EVET" if sanction_hits else "hayır",
        )
        await self._publish(analyzed)

    async def _publish(self, analyzed: dict[str, Any]) -> None:
        self.analyzed.append(analyzed)
        await self.bus.publish(self.ANALYZED, analyzed, key=str(analyzed["transaction_id"]))
