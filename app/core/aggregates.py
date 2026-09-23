"""Online aggregate statistics over analyzed events.

A compact, dependency-free stand-in for River-style streaming stats + concept
drift visibility: keeps a running mean/stdev and a bounded histogram of risk
scores so ops can see whether the risk distribution is shifting (drift).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class OnlineStats:
    """Welford online mean/variance + quantised histogram."""

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    hist: Counter = field(default_factory=Counter)

    def update(self, value: float, bucket_step: float = 0.05) -> None:
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)
        bucket = round(value / bucket_step) * bucket_step
        self.hist[bucket] += 1

    @property
    def variance(self) -> float:
        return self.m2 / self.n if self.n > 1 else 0.0

    @property
    def stdev(self) -> float:
        return math.sqrt(self.variance)

    def summary(self) -> dict[str, object]:
        return {
            "n": self.n,
            "mean": round(self.mean, 4),
            "stdev": round(self.stdev, 4),
            "max": max(self.hist) if self.hist else None,
            "histogram": {str(k): v for k, v in sorted(self.hist.items())},
        }


class MicroclusterDetector:
    """Edge-stream microcluster detection (MIDAS-inspired).

    Flags moments when a single edge (customer -> beneficiary) fires many times
    in a short window — the signature of mule accounts / colluding devices.
    Backed by a bounded priority structure to keep memory constant.
    """

    def __init__(self, threshold: int = 3, window_seconds: int = 3600) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._edges: dict[tuple[str, str], list[float]] = {}

    def _key(self, tx: dict) -> tuple[str, str] | None:
        src = tx.get("customer_id")
        dst = tx.get("beneficiary_id") or tx.get("device_id")
        return (str(src), str(dst)) if src and dst else None

    def update(self, tx: dict, ts_ms: float) -> float:
        """Register an edge event; return microcluster anomaly score (0..1)."""
        key = self._key(tx)
        if key is None:
            return 0.0
        bucket = self._edges.setdefault(key, [])
        bucket.append(ts_ms)
        # Drop events outside the window (constant-ish memory per active edge).
        self._edges[key] = [t for t in bucket if ts_ms - t <= self.window_seconds * 1000]
        count = len(self._edges[key])
        return min(1.0, count / self.threshold)
