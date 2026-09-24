"""Entity-graph external signal (hot path)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.features.extractor import Extraction
from app.graph.entity_graph import EntityGraph
from app.scoring.reasons import ReasonCode
from app.scoring.signals import SignalResult


class GraphSignal:
    name = "graph"

    def __init__(self, graph: EntityGraph) -> None:
        self.graph = graph

    def evaluate(
        self, tx: dict[str, Any], extraction: Extraction, features: Mapping[str, float]
    ) -> SignalResult:
        view = extraction.tx
        signals = self.graph.observe(
            tx_id=view.transaction_id,
            customer_id=view.customer_id,
            ts=view.epoch,
            amount=view.amount_try,
            beneficiary=view.beneficiary,
            device=view.device_id,
            ip=view.ip_address,
        )
        reasons = [
            ReasonCode(code, text, "signal", weight) for code, text, weight in signals.reasons
        ]
        details = {
            "score": signals.score,
            "pass_through": round(signals.pass_through, 4),
            "fan_in_30m": signals.fan_in_30m,
            "inflow_30m": round(signals.inflow_30m, 2),
            "cycle": signals.cycle,
            "fraud_distance": signals.fraud_distance,
            "known_mule_payee": signals.known_mule_payee,
            "shared_fraud_device": signals.shared_fraud_device,
            "ring_id": signals.ring_id,
        }
        return SignalResult(signals.score, reasons, details, signals.features())
