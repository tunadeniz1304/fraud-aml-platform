"""Online aggregate statistics over analyzed events.

A compact, dependency-free stand-in for River-style streaming stats + concept
drift visibility: keeps a running mean/stdev and a bounded histogram of risk
scores so ops can see whether the risk distribution is shifting (drift).
"""

from __future__ import annotations

import math
from collections import Counter, OrderedDict, deque
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

    Memory is bounded (bug #11): per-edge timestamps live in a deque trimmed
    to the window, idle edges are swept periodically and the number of tracked
    edges is capped with LRU eviction.
    """

    def __init__(
        self,
        threshold: int = 3,
        window_seconds: int = 3600,
        max_edges: int = 100_000,
        sweep_every: int = 1_000,
    ) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.max_edges = max_edges
        self.sweep_every = sweep_every
        self._edges: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._updates = 0

    def _key(self, tx: dict) -> tuple[str, str] | None:
        # Only real payment edges count: (customer, device) repeating is normal
        # behaviour and must not add baseline noise to every transfer.
        src = tx.get("customer_id")
        dst = tx.get("beneficiary_id")
        return (str(src), str(dst)) if src and dst else None

    def _sweep(self, now_ms: float) -> None:
        cutoff = now_ms - self.window_seconds * 1000
        stale = [k for k, q in self._edges.items() if not q or q[-1] < cutoff]
        for key in stale:
            del self._edges[key]

    def __len__(self) -> int:
        return len(self._edges)

    def update(self, tx: dict, ts_ms: float) -> float:
        """Register an edge event; return microcluster anomaly score (0..1)."""
        key = self._key(tx)
        if key is None:
            return 0.0
        self._updates += 1
        bucket = self._edges.get(key)
        if bucket is None:
            bucket = deque()
            self._edges[key] = bucket
        else:
            self._edges.move_to_end(key)
        bucket.append(ts_ms)
        cutoff = ts_ms - self.window_seconds * 1000
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if self._updates % self.sweep_every == 0:
            self._sweep(ts_ms)
        while len(self._edges) > self.max_edges:
            self._edges.popitem(last=False)
        return min(1.0, len(bucket) / self.threshold)
