"""Model monitoring (P1.6): PSI drift of the score and top features.

The champion's metadata stores reference bin edges + shares (validation
split). Live scored events are appended to bounded windows; every
``compute_every`` events the PSI per monitored series is recomputed and
exported as ``fraud_drift_psi{feature=…}``. PSI > 0.1 = watch, > 0.25 =
significant drift (alert).

**Reference.** In production the reference is the champion's validation
distribution (``source="model"``). A demo population is not drawn from the
training distribution (6 days of history instead of 60, rebased timestamps),
so comparing it with the model reference raised false alarms at startup
(``device_age_d`` PSI 7.7). Outside production the pipeline therefore replays
the demo population — the same file the demo stream plays — once through the
live engine at startup and installs that distribution as the reference
(``source="population"``, :meth:`DriftMonitor.replace_reference`); until it is
ready the monitor is *pending* and ignores observations. The startup warm-up
batch initialises state and is never observed. A real shift (e.g.
``STREAM_DRIFT``) still alarms.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from typing import Any

from app.ml.metrics import distribution, psi_from_distribution, reference_bins
from app.monitoring import metrics


def score_edges() -> list[float]:
    """Fixed risk bands for the score series (see ``drift_score_edges``)."""
    from app.config import get_settings

    return list(get_settings().drift_score_edges)


WATCH, ALERT = 0.10, 0.25


class DriftMonitor:
    def __init__(
        self,
        reference: Mapping[str, Any],
        *,
        window: int = 5000,
        compute_every: int = 500,
        pending: bool = False,
    ) -> None:
        self.reference = {k: v for k, v in reference.items() if v.get("edges") is not None}
        self.windows: dict[str, deque[float]] = {k: deque(maxlen=window) for k in self.reference}
        self.compute_every = compute_every
        self.source = "model"
        #: waiting for :meth:`replace_reference` — observations are ignored
        self.pending = pending
        self.observed = 0
        self.last: dict[str, float] = {}
        self._lock = threading.Lock()

    def replace_reference(self, samples: Mapping[str, list[float]], source: str) -> None:
        """Rebuild edges + shares for every monitored series from ``samples``."""
        with self._lock:
            for name in list(self.reference):
                values = samples.get(name) or []
                edges = score_edges() if name == "score" else reference_bins(values)
                self.reference[name] = {"edges": edges, "share": distribution(values, edges)}
                self.windows[name].clear()
            self.source = source
            self.pending = False
            self.last = {}

    def observe(self, features: Mapping[str, float], score: float) -> None:
        with self._lock:
            if self.pending:
                return
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
            if self.pending:
                return {}
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
            "reference": self.source,
            "pending": self.pending,
        }
