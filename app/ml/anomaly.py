"""Unsupervised anomaly scoring for the hot path (IsolationForest + ECOD).

Both detectors are *trained* with the reference libraries (scikit-learn's
``IsolationForest``; ECOD follows ``pyod``'s formulation) and *served* from
plain arrays so a single-row score costs well under a millisecond:

* :class:`FastIsolationForest` walks the exported tree arrays in pure Python
  and reproduces ``IsolationForest.score_samples`` exactly (tested);
* :class:`ECODScorer` keeps the sorted training columns and uses
  ``bisect`` for the empirical CDF tails (rank-consistent with ``pyod.ECOD``).

Raw scores are mapped to ``[0, 1]`` percentiles of the training (normal)
distribution (:class:`QuantileCalibrator`) and averaged into one anomaly
signal that the stacker and the policy layer consume.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


def _avg_path_length(n: float) -> float:
    """c(n): average path length of an unsuccessful BST search (sklearn formula)."""
    if n <= 1:
        return 0.0
    if n <= 2:
        return 1.0
    return 2.0 * (math.log(n - 1.0) + np.euler_gamma) - 2.0 * (n - 1.0) / n


@dataclass
class _Tree:
    left: list[int]
    right: list[int]
    feature: list[int]
    threshold: list[float]
    leaf_depth: list[float]


class FastIsolationForest:
    """Pure-Python single-row scorer for a fitted ``sklearn`` IsolationForest."""

    def __init__(self, trees: list[_Tree], max_samples: int) -> None:
        self.trees = trees
        self.max_samples = max_samples
        self._denominator = len(trees) * _avg_path_length(max_samples)

    @classmethod
    def from_sklearn(cls, model: Any) -> FastIsolationForest:
        trees: list[_Tree] = []
        for est in model.estimators_:
            t = est.tree_
            n = t.node_count
            depth = [0] * n
            for node in range(n):  # parents precede children in sklearn trees
                for child in (t.children_left[node], t.children_right[node]):
                    if child != -1:
                        depth[child] = depth[node] + 1
            leaf_depth = [
                float(depth[i] + _avg_path_length(float(t.n_node_samples[i])))
                if t.children_left[i] == -1
                else 0.0
                for i in range(n)
            ]
            trees.append(
                _Tree(
                    t.children_left.tolist(),
                    t.children_right.tolist(),
                    t.feature.tolist(),
                    t.threshold.tolist(),
                    leaf_depth,
                )
            )
        return cls(trees, int(model.max_samples_))

    def raw_score(self, row: Sequence[float]) -> float:
        """Equals ``IsolationForest.score_samples`` (higher = more normal)."""
        x = np.asarray(row, dtype=np.float32).tolist()  # sklearn compares in float32
        total = 0.0
        for tree in self.trees:
            node = 0
            left, right, feat, thr = tree.left, tree.right, tree.feature, tree.threshold
            while left[node] != -1:
                node = left[node] if x[feat[node]] <= thr[node] else right[node]
            total += tree.leaf_depth[node]
        return -(2.0 ** (-total / self._denominator))

    def anomaly(self, row: Sequence[float]) -> float:
        """Higher = more anomalous."""
        return -self.raw_score(row)

    # --- persistence ---------------------------------------------------------------
    def save(self, path: Path) -> None:
        arrays: dict[str, Any] = {"max_samples": np.array([self.max_samples])}
        offsets = [0]
        for tree in self.trees:
            offsets.append(offsets[-1] + len(tree.left))
        arrays["offsets"] = np.array(offsets, dtype=np.int64)
        arrays["left"] = np.concatenate([np.array(t.left, dtype=np.int32) for t in self.trees])
        arrays["right"] = np.concatenate([np.array(t.right, dtype=np.int32) for t in self.trees])
        arrays["feature"] = np.concatenate(
            [np.array(t.feature, dtype=np.int32) for t in self.trees]
        )
        arrays["threshold"] = np.concatenate([np.array(t.threshold) for t in self.trees])
        arrays["leaf_depth"] = np.concatenate([np.array(t.leaf_depth) for t in self.trees])
        np.savez_compressed(path, **arrays)

    @classmethod
    def load(cls, path: Path) -> FastIsolationForest:
        with np.load(path) as data:
            offsets = data["offsets"].tolist()
            cols = {k: data[k] for k in ("left", "right", "feature", "threshold", "leaf_depth")}
            max_samples = int(data["max_samples"][0])
        trees = [
            _Tree(
                cols["left"][a:b].tolist(),
                cols["right"][a:b].tolist(),
                cols["feature"][a:b].tolist(),
                cols["threshold"][a:b].tolist(),
                cols["leaf_depth"][a:b].tolist(),
            )
            for a, b in itertools.pairwise(offsets)
        ]
        return cls(trees, max_samples)


class ECODScorer:
    """Empirical-CDF outlier detection (Li et al., 2022) served with ``bisect``."""

    def __init__(self, columns: list[list[float]], skew_sign: list[float]) -> None:
        self.columns = columns  # sorted training values per feature
        self.skew_sign = skew_sign
        self.n = len(columns[0]) if columns else 0

    @classmethod
    def fit(cls, x: np.ndarray) -> ECODScorer:
        from scipy.stats import skew

        cols = [sorted(x[:, j].tolist()) for j in range(x.shape[1])]
        with np.errstate(all="ignore"):
            sk = np.nan_to_num(skew(x, axis=0))
        return cls(cols, np.sign(sk).tolist())

    def score(self, row: Sequence[float]) -> float:
        n = self.n
        floor = 1.0 / (n + 1.0)
        total = 0.0
        for j, value in enumerate(row):
            col = self.columns[j]
            left = max(floor, bisect.bisect_right(col, value) / n)
            right = max(floor, (n - bisect.bisect_left(col, value)) / n)
            u_l, u_r = -math.log(left), -math.log(right)
            s = self.skew_sign[j]
            u_skew = u_l if s < 0 else (u_r if s > 0 else u_l + u_r)
            total += max(u_l, u_r, u_skew)
        return total

    def save(self, path: Path) -> None:
        np.savez_compressed(
            path, columns=np.array(self.columns, dtype=float), skew=np.array(self.skew_sign)
        )

    @classmethod
    def load(cls, path: Path) -> ECODScorer:
        with np.load(path) as data:
            return cls(data["columns"].tolist(), data["skew"].tolist())


class QuantileCalibrator:
    """Raw score -> percentile in the reference (normal) score distribution."""

    def __init__(self, quantiles: Sequence[float]) -> None:
        self.quantiles = list(quantiles)
        self.levels = [i / (len(self.quantiles) - 1) for i in range(len(self.quantiles))]

    @classmethod
    def fit(cls, scores: Sequence[float], points: int = 201) -> QuantileCalibrator:
        qs = np.quantile(np.asarray(scores, dtype=float), np.linspace(0, 1, points))
        return cls(np.maximum.accumulate(qs).tolist())

    def __call__(self, score: float) -> float:
        q = self.quantiles
        if score <= q[0]:
            return 0.0
        if score >= q[-1]:
            return 1.0
        i = bisect.bisect_right(q, score)
        lo, hi = q[i - 1], q[i]
        frac = 0.0 if hi == lo else (score - lo) / (hi - lo)
        return self.levels[i - 1] + frac * (self.levels[i] - self.levels[i - 1])

    def to_list(self) -> list[float]:
        return self.quantiles


class AnomalyDetector:
    """IForest + ECOD ensemble -> one calibrated anomaly score in ``[0, 1]``."""

    def __init__(
        self,
        iforest: FastIsolationForest,
        ecod: ECODScorer,
        cal_iforest: QuantileCalibrator,
        cal_ecod: QuantileCalibrator,
    ) -> None:
        self.iforest = iforest
        self.ecod = ecod
        self.cal_iforest = cal_iforest
        self.cal_ecod = cal_ecod

    def components(self, row: Sequence[float]) -> tuple[float, float]:
        return self.cal_iforest(self.iforest.anomaly(row)), self.cal_ecod(self.ecod.score(row))

    def score(self, row: Sequence[float]) -> float:
        a, b = self.components(row)
        return 0.5 * (a + b)
