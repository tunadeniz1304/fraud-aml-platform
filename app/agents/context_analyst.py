"""ContextAnalyst agent.

Consumes monitored transfers and scores them with the hybrid
:class:`~app.scoring.engine.ScoringEngine` (rules + LightGBM + anomaly +
external signals → policy). Guarantees:

* the synchronous path never calls an LLM (bug #9);
* amounts are normalised to TRY before any comparison (bug #12);
* sanctions, country, IP, channel and purpose signals feed the score and a
  sanctions hit forces a HOLD + case (bug #6);
* transfers of an already ``BLOKE`` account are stopped *before* scoring
  (bug #5); unknown customers go to review instead of a silent ``1.0`` (bug #8);
* all buffers are bounded (bug #11) — windows live in the feature store;
* the adaptive profile only learns from ALLOW decisions (poisoning protection).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.aggregates import OnlineStats
from app.core.event_bus import EventBus
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.monitoring import metrics, tracing
from app.scoring.engine import ScoringEngine

logger = logging.getLogger("fraud.analyst")

StatusLookup = Callable[[str], str | None]


class ContextAnalyst:
    """Second stage: hybrid scoring + policy (synchronous, no LLM)."""

    MONITORED = "transaction.monitored"
    ANALYZED = "transaction.analyzed"

    def __init__(
        self,
        bus: EventBus,
        customers_path: Path | None = None,
        *,
        account_status: StatusLookup | None = None,
        extractor: FeatureExtractor | None = None,
        engine: ScoringEngine | None = None,
        buffer: int | None = None,
    ) -> None:
        settings = get_settings()
        self.bus = bus
        if engine is None:
            if extractor is None:
                directory = CustomerDirectory.from_file(
                    customers_path or settings.resolved_customers_path
                )
                extractor = FeatureExtractor(customers=directory)
            engine = ScoringEngine.from_settings(extractor)
        self.engine = engine
        self.extractor = engine.extractor
        self.directory = engine.customers
        self._peer_cache = self._peer_averages(self.directory.records())
        self.analyzed: deque[dict[str, Any]] = deque(maxlen=buffer or settings.analyzed_buffer)
        self.stats = OnlineStats()
        self.account_status = account_status
        #: off while the startup warm-up batch initialises state (not live traffic)
        self.observe_drift = True
        self.bus.subscribe(self.MONITORED, self._on_monitored)

    @property
    def sanctions(self) -> Any:
        return self.engine.sanctions

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _peer_avg(records: list[dict[str, Any]], record: dict[str, Any]) -> float:
        """Average amount of other customers from the same home city (peer group)."""
        city = record.get("home_city", "")
        peers = [
            float(r["avg_amount"])
            for r in records
            if r.get("home_city") == city and r["customer_id"] != record["customer_id"]
        ]
        return sum(peers) / len(peers) if peers else float(record["avg_amount"])

    @staticmethod
    def _peer_averages(records: list[dict[str, Any]]) -> dict[str, float]:
        """Average amount of the other customers from the same home city (peer group)."""
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

    # --- event handler ---------------------------------------------------------
    async def _on_monitored(self, tx: dict[str, Any]) -> None:
        customer_id = str(tx["customer_id"])
        status = self.account_status(customer_id) if self.account_status else None
        # snapshot → score → commit of one customer is serialised (no lost velocity)
        async with self.extractor.store.customer_lock(customer_id):
            with tracing.span("scoring.score", transaction_id=tx.get("transaction_id")):
                result = await self.engine.score(tx, account_status=status)
            if result.scored:
                await self.engine.commit(result)
        analyzed = {**tx, **result.event_fields()}
        if result.flags.get("account_blocked"):
            logger.warning(
                "[Analyst] %s: hesap %s zaten BLOKE — işlem skorlanmadan durduruldu",
                tx["transaction_id"],
                customer_id,
            )
        elif result.flags.get("unknown_customer"):
            logger.warning(
                "[Analyst] %s: bilinmeyen müşteri %s — skorlanamadı, incelemeye yönlendirildi",
                tx["transaction_id"],
                customer_id,
            )
        else:
            self.stats.update(result.risk_score)
            if self.engine.drift is not None and self.observe_drift:
                self.engine.drift.observe(result.features, result.policy.stacked)
            metrics.SCORE_LATENCY.observe(result.latency_ms / 1000)
            if result.model is not None:
                metrics.MODEL_SCORE.labels(result.model_version, "champion").observe(
                    result.model.ml
                )
            if result.challenger_score is not None and result.challenger_version:
                metrics.MODEL_SCORE.labels(result.challenger_version, "challenger").observe(
                    result.challenger_score
                )
            peer = self._peer_cache.get(customer_id) or 0.0
            analyzed["peer_group_avg"] = round(peer, 2) if peer else None
            analyzed["peer_amount_ratio"] = round(result.amount_try / peer, 3) if peer > 0 else None
            logger.log(
                logging.DEBUG if result.decision == "ALLOW" else logging.INFO,
                "[Analyst] %s risk %.2f → %s (kural %.2f, ML %s, yaptırım=%s, %.1f ms)",
                tx["transaction_id"],
                result.risk_score,
                result.decision,
                result.rules.score if result.rules else 0.0,
                f"{result.model.ml:.2f}" if result.model else "-",
                "EVET" if result.sanctions else "hayır",
                result.latency_ms,
            )
        await self._publish(analyzed)

    async def _publish(self, analyzed: dict[str, Any]) -> None:
        self.analyzed.append(analyzed)
        await self.bus.publish(self.ANALYZED, analyzed, key=str(analyzed["transaction_id"]))
