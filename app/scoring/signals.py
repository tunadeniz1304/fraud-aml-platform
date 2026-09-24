"""External signal hooks for the policy layer.

An external signal is computed next to (not inside) the ML model and enters
the risk as ``1 - Π(1 - w_i · s_i)`` with a configured weight. F3 ships the
edge-burst (MIDAS-style microcluster) signal; the entity graph, APP scam
engine and consortium deny-list plug in through the same protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.aggregates import MicroclusterDetector
from app.features.extractor import Extraction
from app.scoring.reasons import ReasonCode


@dataclass
class SignalResult:
    score: float
    reasons: list[ReasonCode] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    #: extra values exposed to rules (``register_external`` names)
    features: dict[str, float] = field(default_factory=dict)


class ExternalSignal(Protocol):
    name: str

    def evaluate(self, tx: dict[str, Any], extraction: Extraction) -> SignalResult: ...


class BurstSignal:
    """Same customer→beneficiary edge firing repeatedly within the window."""

    name = "burst"

    def __init__(self, detector: MicroclusterDetector | None = None) -> None:
        self.detector = detector or MicroclusterDetector()

    def evaluate(self, tx: dict[str, Any], extraction: Extraction) -> SignalResult:
        score = self.detector.update(tx, extraction.tx.epoch * 1000)
        reasons = []
        if score >= 1.0:
            reasons.append(
                ReasonCode(
                    "SIG_EDGE_BURST",
                    "Aynı alıcıya kısa sürede tekrarlanan transfer patlaması",
                    "signal",
                    score,
                )
            )
        return SignalResult(round(score, 4), reasons, {"microcluster": round(score, 4)})
