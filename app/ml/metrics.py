"""Model evaluation metrics and drift statistics.

* ``pr_auc`` (average precision) — the headline metric for rare-event fraud;
* ``recall_at_fpr`` — fraud caught when at most X% of clean traffic is flagged;
* ``budget_metrics`` — with a fixed alert budget (top-k% riskiest), share of
  fraud *amount* caught (cost-weighted recall) and alert precision;
* ``psi`` — population stability index for feature/score drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import ArrayLike


def pr_auc(y: ArrayLike, scores: ArrayLike) -> float:
    from sklearn.metrics import average_precision_score

    return float(average_precision_score(y, scores))


def roc_auc(y: ArrayLike, scores: ArrayLike) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, scores))


def recall_at_fpr(y: ArrayLike, scores: ArrayLike, fpr: float = 0.01) -> float:
    ya, sa = np.asarray(y), np.asarray(scores, dtype=float)
    negatives = sa[ya == 0]
    positives = sa[ya == 1]
    if len(negatives) == 0 or len(positives) == 0:
        return 0.0
    threshold = float(np.quantile(negatives, 1.0 - fpr, method="higher"))
    return float((positives > threshold).mean())


def budget_metrics(
    y: ArrayLike,
    scores: ArrayLike,
    amounts: ArrayLike,
    budget: float = 0.01,
) -> dict[str, float]:
    ya = np.asarray(y)
    sa = np.asarray(scores, dtype=float)
    aa = np.asarray(amounts, dtype=float)
    k = max(1, round(len(sa) * budget))
    top = np.argsort(-sa, kind="stable")[:k]
    fraud_amount = float(aa[ya == 1].sum())
    caught_amount = float(aa[top][ya[top] == 1].sum())
    return {
        "alert_budget": budget,
        "alerts": int(k),
        "precision": float(ya[top].mean()),
        "recall": float(ya[top].sum() / max(1, ya.sum())),
        "cost_weighted_recall": caught_amount / fraud_amount if fraud_amount else 0.0,
    }


def evaluate(y: Sequence[int], scores: Sequence[float], amounts: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(y),
        "positives": int(np.sum(y)),
        "pr_auc": round(pr_auc(y, scores), 4),
        "roc_auc": round(roc_auc(y, scores), 4),
        "recall_at_1pct_fpr": round(recall_at_fpr(y, scores, 0.01), 4),
        "budget_1pct": {k: round(v, 4) for k, v in budget_metrics(y, scores, amounts).items()},
    }


def reference_bins(values: Sequence[float], bins: int = 10) -> list[float]:
    """Quantile bin edges of a reference distribution (inner edges only)."""
    qs = np.quantile(np.asarray(values, dtype=float), np.linspace(0, 1, bins + 1)[1:-1])
    return sorted(set(np.round(qs, 8).tolist()))


#: additive (Laplace) smoothing so empty bins never give log(0) or a division by zero
PSI_EPSILON = 1e-4


def _hist(values: Sequence[float], edges: Sequence[float]) -> np.ndarray:
    idx = np.searchsorted(np.asarray(edges), np.asarray(values, dtype=float), side="right")
    counts = np.bincount(idx, minlength=len(edges) + 1).astype(float)
    return counts / max(1.0, counts.sum())


def _smooth(share: np.ndarray) -> np.ndarray:
    smoothed = np.asarray(share, dtype=float) + PSI_EPSILON
    return smoothed / smoothed.sum()


def psi(
    expected: Sequence[float], actual: Sequence[float], edges: Sequence[float] | None = None
) -> float:
    """Population stability index (0.1 = watch, 0.25 = significant drift)."""
    edges = list(edges) if edges is not None else reference_bins(expected)
    e = _smooth(_hist(expected, edges))
    a = _smooth(_hist(actual, edges))
    return float(np.sum((a - e) * np.log(a / e)))


def psi_from_distribution(
    expected_share: Sequence[float], actual: Sequence[float], edges: Sequence[float]
) -> float:
    e = _smooth(np.asarray(expected_share, dtype=float))
    a = _smooth(_hist(actual, edges))
    return float(np.sum((a - e) * np.log(a / e)))


def distribution(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    return _hist(values, edges).tolist()
