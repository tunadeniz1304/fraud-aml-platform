"""Online behavioural anomaly model (P1.1, ARIC-style adaptive analytics).

A ``river`` **Half-Space Trees** model scores every transfer on a compact,
[0, 1]-scaled view of its behavioural features and learns *online* — but only
from trusted events (ALLOW decisions or analyst-labelled clean transfers), so
fraudsters cannot teach it that their behaviour is normal (poisoning
protection). Until it has seen a full window of trusted events it abstains
(score 0).

Raw Half-Space-Tree scores are not calibrated (normal traffic often scores
0.7–0.9), so the signal is the *percentile* of the raw score among recently
learned trusted events, and only the extreme tail contributes:
``signal = clip((percentile - 0.9) / 0.1, 0, 1)``.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from app.config import get_settings
from app.features.extractor import Extraction
from app.scoring.reasons import ReasonCode
from app.scoring.signals import SignalResult

_BINS = 200


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def behaviour_vector(features: Mapping[str, float]) -> dict[str, float]:
    hour = features.get("hour", 12.0)
    ltt = features.get("login_to_transfer_s", -1.0)
    return {
        "amount_z": _clip01((features.get("amount_zscore", 0.0) + 3.0) / 9.0),
        "hour_sin": 0.5 + 0.5 * math.sin(2 * math.pi * hour / 24),
        "hour_cos": 0.5 + 0.5 * math.cos(2 * math.pi * hour / 24),
        "velocity": _clip01(features.get("cnt_1h", 0.0) / 20.0),
        "payees": _clip01(features.get("distinct_payees_24h", 0.0) / 10.0),
        "typing": _clip01(features.get("typing_deviation", 0.0) / 8.0),
        "login": 0.5 if ltt < 0 else _clip01(math.log1p(ltt) / math.log1p(3600)),
        "new_device": features.get("is_new_device", 0.0),
        "new_payee": features.get("is_new_payee", 0.0),
        "shared_device": _clip01((features.get("device_customers_7d", 1.0) - 1.0) / 4.0),
    }


class OnlineAnomalySignal:
    name = "online"

    def __init__(self, *, n_trees: int = 15, height: int = 8, window: int = 250, seed: int = 42):
        from river.anomaly import HalfSpaceTrees

        self.model = HalfSpaceTrees(n_trees=n_trees, height=height, window_size=window, seed=seed)
        self.window = window
        self.learned = 0
        self._pending: OrderedDict[str, tuple[dict[str, float], float]] = OrderedDict()
        self._lock = threading.Lock()
        self._hist = [0] * _BINS  # raw scores of trusted events (calibration)
        self._hist_n = 0

    @property
    def ready(self) -> bool:
        return self.learned >= self.window

    def evaluate(
        self, tx: dict[str, Any], extraction: Extraction, features: Mapping[str, float]
    ) -> SignalResult:
        x = behaviour_vector(features)
        with self._lock:
            raw = float(self.model.score_one(x))
            self._pending[extraction.tx.transaction_id] = (x, raw)
            while len(self._pending) > 10_000:
                self._pending.popitem(last=False)
            score = self._calibrated(raw) if self.ready else 0.0
        reasons = []
        if score >= get_settings().online_anomaly_reason_min:
            reasons.append(
                ReasonCode(
                    "ONLINE_ANOMALY",
                    "Müşteri davranışı, çevrimiçi öğrenen profile göre olağandışı",
                    "signal",
                    score * 0.5,
                )
            )
        return SignalResult(
            round(score, 4),
            reasons,
            {"score": round(score, 4), "raw": round(raw, 4), "ready": self.ready},
            {"online_anomaly": round(score, 4)},
        )

    def _calibrated(self, raw: float) -> float:
        if not self._hist_n:
            return 0.0
        b = min(_BINS - 1, max(0, int(raw * _BINS)))
        below = sum(self._hist[:b]) + 0.5 * self._hist[b]
        percentile = below / self._hist_n
        return max(0.0, min(1.0, (percentile - 0.9) / 0.1))

    def learn(self, transaction_id: str) -> bool:
        """Learn from a trusted event previously scored with :meth:`evaluate`."""
        with self._lock:
            entry = self._pending.pop(transaction_id, None)
            if entry is None:
                return False
            x, raw = entry
            self.model.learn_one(x)
            self.learned += 1
            self._hist[min(_BINS - 1, max(0, int(raw * _BINS)))] += 1
            self._hist_n += 1
            return True
