"""Model monitoring (P1.6): PSI drift of the score and top features.

The champion's metadata stores reference bin edges + shares (validation
split). Live scored events are appended to bounded windows; every
``compute_every`` events the PSI per monitored series is recomputed and
exported as ``fraud_drift_psi{feature=…}``. PSI > 0.1 = watch, > 0.25 =
significant drift (alert).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from typing import Any

from app.ml.metrics import psi_from_distribution
from app.monitoring import metrics

WATCH, ALERT = 0.10, 0.25


class DriftMonitor:
    def __init__(
        self, reference: Mapping[str, Any], *, window: int = 5000, compute_every: int = 500
    ) -> None:
        self.reference = {k: v for k, v in reference.items() if v.get("edges") is not None}
        self.windows: dict[str, deque[float]] = {k: deque(maxlen=window) for k in self.reference}
        self.compute_every = compute_every
        self.observed = 0
        self.last: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, features: Mapping[str, float], score: float) -> None:
        with self._lock:
            for name, window in self.windows.items():
                value = score if name == "score" else features.get(name)
                if value is not None:
                    window.append(float(value))
            self.observed += 1
            due = self.observed % self.compute_every == 0
        if due:
            self.compute()

    def compute(self) -> dict[str, float]:
        with self._lock:
            out = {}
            for name, window in self.windows.items():
                if len(window) < 50:
                    continue
                ref = self.reference[name]
                value = round(psi_from_distribution(ref["share"], list(window), ref["edges"]), 4)
                out[name] = value
                metrics.DRIFT_PSI.labels(feature=name).set(value)
            self.last = out
            return out

    def status(self) -> dict[str, Any]:
        psi = self.compute() or self.last
        return {
            "observed": self.observed,
            "psi": psi,
            "watch": sorted(k for k, v in psi.items() if WATCH < v <= ALERT),
            "alerts": sorted(k for k, v in psi.items() if v > ALERT),
            "thresholds": {"watch": WATCH, "alert": ALERT},
        }
