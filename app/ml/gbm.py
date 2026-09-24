"""LightGBM fraud classifier with native TreeSHAP explanations.

The hot path calls :meth:`GBMModel.probability` (~0.1 ms) for every
transaction and :meth:`GBMModel.explain` (~2 ms) only when a decision needs
an explanation. LightGBM's ``pred_contrib=True`` returns the exact TreeSHAP
values (identical to ``shap.TreeExplainer`` — asserted in the test-suite) plus
the expected value; their sum is the raw log-odds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.ml.stacker import sigmoid

DEFAULT_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 20,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,
    "verbosity": -1,
}


@dataclass
class Explanation:
    probability: float
    contributions: dict[str, float]
    expected_value: float


class GBMModel:
    def __init__(self, booster: Any, feature_names: Sequence[str]) -> None:
        self.booster = booster
        self.feature_names = list(feature_names)

    @classmethod
    def train(
        cls,
        x_train: np.ndarray,
        y_train: np.ndarray,
        x_valid: np.ndarray,
        y_valid: np.ndarray,
        feature_names: Sequence[str],
        *,
        seed: int = 42,
        num_boost_round: int = 300,
        early_stopping: int = 40,
    ) -> GBMModel:
        import lightgbm as lgb

        params = {**DEFAULT_PARAMS, "seed": seed}
        train = lgb.Dataset(x_train, y_train, feature_name=list(feature_names))
        valid = lgb.Dataset(x_valid, y_valid, reference=train)
        booster = lgb.train(
            params,
            train,
            num_boost_round=num_boost_round,
            valid_sets=[valid],
            callbacks=[lgb.early_stopping(early_stopping, verbose=False)],
        )
        return cls(booster, feature_names)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.booster.predict(x, num_iteration=self.best_iteration))

    @property
    def best_iteration(self) -> int | None:
        best = getattr(self.booster, "best_iteration", 0)
        return best if best and best > 0 else None

    def contributions(self, x: np.ndarray, *, num_threads: int = 0) -> np.ndarray:
        return np.asarray(
            self.booster.predict(
                x, pred_contrib=True, num_iteration=self.best_iteration, num_threads=num_threads
            )
        )

    # Single-row calls run single-threaded: OpenMP start-up costs more than the
    # work itself for one row and hurts tail latency under load.
    def probability(self, row: Sequence[float]) -> float:
        x = np.asarray([row], dtype=float)
        return float(self.booster.predict(x, num_iteration=self.best_iteration, num_threads=1)[0])

    def explain(self, row: Sequence[float]) -> Explanation:
        contrib = self.contributions(np.asarray([row], dtype=float), num_threads=1)[0]
        raw = float(contrib.sum())
        return Explanation(
            probability=sigmoid(raw),
            contributions=dict(zip(self.feature_names, contrib[:-1].tolist(), strict=True)),
            expected_value=float(contrib[-1]),
        )

    def feature_importance(self) -> dict[str, float]:
        gains = self.booster.feature_importance(importance_type="gain")
        total = float(gains.sum()) or 1.0
        return {
            n: round(float(g) / total, 5) for n, g in zip(self.feature_names, gains, strict=True)
        }

    # Model text goes through Python I/O: LightGBM's C file API cannot open
    # non-ASCII paths on Windows (e.g. "Masaüstü").
    def save(self, path: Path) -> None:
        text = self.booster.model_to_string(num_iteration=self.best_iteration)
        path.write_text(text, encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: Path) -> GBMModel:
        import lightgbm as lgb

        booster = lgb.Booster(model_str=path.read_text(encoding="utf-8"))
        return cls(booster, booster.feature_name())
