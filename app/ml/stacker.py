"""Logistic stacker: maps rule, ML and anomaly scores to one fraud probability.

Fitted on the *validation* split, never on the data the base models saw.
The inputs go through a logit transform, so a rule score of 0.99 and one of
0.999 stay distinguishable. The fit is a multivariate Platt scaling, i.e. a
maximum-likelihood logistic regression over the logits. That makes the output
a calibrated probability *on the validation distribution*. Brier and ECE on the
test period are reported by the training / validation code.

The only constraint is **non-negativity**: every coefficient is ``>= 0``, so a
new rule hit or a higher anomaly can never lower the risk (monotonicity). A
coefficient may be exactly 0, which means that component is not used.

The earlier version floored the coefficients (``>= 0.05``, rule ``>= 0.3``).
That forced every component in regardless of evidence, so it is gone. Rules
still keep material influence through the policy's deterministic action
floors. The floors survive only as the ``floored_legacy`` variant of the
ablation, for comparison.

Input selection (anomaly or not) is a validation-only decision. The
validation rows arrive in time order: the stacker is fitted on the first half
and scored by log-loss on the second half. The anomaly input is kept only if
it lowers the held-out log-loss. The chosen form is then refitted on the whole
validation split. The ablation is stored with the stacker (``fit`` block) and
rendered into the model card.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

INPUTS = ("rule", "ml", "anomaly")
_EPS = 1e-3
#: small L2 penalty on the coefficients (not the intercept): keeps the fit finite
#: when an input separates the validation labels perfectly
L2 = 1e-3
#: minimum positives in each validation half for the input-selection ablation
MIN_HALF_POSITIVES = 5
#: the anomaly input must lower held-out log-loss by at least this much
SELECTION_TOLERANCE = 1e-5


def logit(p: float) -> float:
    p = min(1.0 - _EPS, max(_EPS, p))
    return math.log(p / (1.0 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def logits(rows: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
    a = np.clip(np.asarray(rows, dtype=float), _EPS, 1.0 - _EPS)
    return np.log(a / (1.0 - a))


def fit_logistic(
    x: np.ndarray, y: np.ndarray, *, nonneg: bool = True, l2: float = L2
) -> tuple[np.ndarray, float]:
    """Maximum-likelihood logistic regression (mean log-loss + ``l2/2·|w|²``).

    ``nonneg`` bounds every coefficient at ``>= 0`` (L-BFGS-B); the intercept is
    free, so the mean prediction matches the base rate at the optimum."""
    from scipy.optimize import minimize

    n, d = x.shape
    yf = y.astype(float)

    def loss(theta: np.ndarray) -> tuple[float, np.ndarray]:
        w, b = theta[:d], theta[d]
        z = x @ w + b
        # log(1 + e^z) - y·z, numerically stable
        value = float(np.mean(np.logaddexp(0.0, z) - yf * z)) + 0.5 * l2 * float(w @ w)
        p = 0.5 * (1.0 + np.tanh(0.5 * z))
        r = (p - yf) / n
        grad = np.empty(d + 1)
        grad[:d] = x.T @ r + l2 * w
        grad[d] = r.sum()
        return value, grad

    prior = float(np.clip(yf.mean(), 1e-6, 1 - 1e-6))
    theta0 = np.zeros(d + 1)
    theta0[d] = math.log(prior / (1 - prior))
    bounds = [(0.0, None) if nonneg else (None, None)] * d + [(None, None)]
    res = minimize(loss, theta0, jac=True, method="L-BFGS-B", bounds=bounds)
    return np.asarray(res.x[:d], dtype=float), float(res.x[d])


def _fit_floored_legacy(x: np.ndarray, y: np.ndarray, seed: int) -> tuple[np.ndarray, float]:
    """The pre-v5 fit (coefficients floored at 0.05, rule at 0.3, intercept
    refitted to the base rate). Kept only as an ablation baseline."""
    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(C=1.0, max_iter=1000, random_state=seed).fit(x, y)
    coef = np.maximum(0.05, model.coef_[0])
    coef[0] = max(0.3, float(coef[0]))
    z = x @ coef
    intercept = float(model.intercept_[0])
    target = float(y.mean())
    for _ in range(60):
        p = 1.0 / (1.0 + np.exp(-(z + intercept)))
        grad = float(p.mean()) - target
        if abs(grad) < 1e-7:
            break
        intercept -= grad / max(float((p * (1 - p)).mean()), 1e-6)
    return coef, intercept


def _heldout(x: np.ndarray, y: np.ndarray, coef: np.ndarray, intercept: float) -> dict[str, Any]:
    from sklearn.metrics import average_precision_score

    z = x @ coef + intercept
    p = 0.5 * (1.0 + np.tanh(0.5 * z))
    logloss = float(np.mean(np.logaddexp(0.0, z) - y * z))
    return {
        "log_loss": round(logloss, 6),
        "brier": round(float(np.mean((p - y) ** 2)), 6),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "coef": [round(float(c), 4) for c in coef],
    }


def _ablation(x: np.ndarray, y: np.ndarray, seed: int) -> dict[str, Any] | None:
    """Fit on the first half of the (time-ordered) validation rows, score on the
    second half. Returns ``None`` when a half has too few positives."""
    half = len(y) // 2
    x1, y1, x2, y2 = x[:half], y[:half], x[half:], y[half:]
    if min(int(y1.sum()), int(y2.sum())) < MIN_HALF_POSITIVES or y1.min() == y1.max():
        return None
    no_anomaly = np.array([1.0, 1.0, 0.0])
    variants: dict[str, dict[str, Any]] = {}
    coef, b = _fit_floored_legacy(x1, y1, seed)
    variants["floored_legacy"] = _heldout(x2, y2, coef, b)
    coef, b = fit_logistic(x1, y1, nonneg=False)
    variants["unconstrained"] = _heldout(x2, y2, coef, b)
    coef, b = fit_logistic(x1, y1)
    variants["nonneg"] = _heldout(x2, y2, coef, b)
    coef, b = fit_logistic(x1[:, :2], y1)
    variants["nonneg_no_anomaly"] = _heldout(x2, y2, np.append(coef, 0.0) * no_anomaly, b)
    keep = (
        variants["nonneg"]["log_loss"]
        < variants["nonneg_no_anomaly"]["log_loss"] - SELECTION_TOLERANCE
    )
    return {
        "protocol": "validation first half -> fit, second half -> score (time order)",
        "rows": [half, len(y) - half],
        "positives": [int(y1.sum()), int(y2.sum())],
        "variants": variants,
        "anomaly_kept": bool(keep),
    }


@dataclass(frozen=True)
class Stacker:
    coef: tuple[float, float, float]
    intercept: float
    diagnostics: dict[str, Any] | None = field(default=None, compare=False)

    def predict(self, rule: float, ml: float, anomaly: float) -> float:
        z = self.intercept
        for c, x in zip(self.coef, (rule, ml, anomaly), strict=True):
            z += c * logit(x)
        return sigmoid(z)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "inputs": list(INPUTS),
            "transform": "logit",
            "coef": list(self.coef),
            "intercept": self.intercept,
        }
        if self.diagnostics is not None:
            out["fit"] = self.diagnostics
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stacker:
        c = [float(v) for v in data["coef"]]
        return cls((c[0], c[1], c[2]), float(data["intercept"]), data.get("fit"))

    @classmethod
    def fit(
        cls,
        rows: Sequence[Sequence[float]],
        labels: Sequence[int],
        *,
        seed: int = 0,
        select_inputs: bool = True,
    ) -> Stacker:
        """Non-negative Platt-type fit on time-ordered validation rows."""
        x = logits(rows)
        y = np.asarray(labels, dtype=int)
        ablation = _ablation(x, y, seed) if select_inputs else None
        keep_anomaly = ablation is None or ablation["anomaly_kept"]
        if keep_anomaly:
            coef, intercept = fit_logistic(x, y)
        else:
            c2, intercept = fit_logistic(x[:, :2], y)
            coef = np.append(c2, 0.0)
        diagnostics = {
            "method": "non-negative logistic regression over logits (Platt-type), L2="
            f"{L2:g}, fitted on the validation split",
            "rows": len(y),
            "positives": int(y.sum()),
            "anomaly_input": "kept" if keep_anomaly else "dropped (validation ablation)",
            "ablation": ablation,
        }
        c = [round(float(v), 6) for v in coef]
        return cls((c[0], c[1], c[2]), round(intercept, 6), diagnostics)
