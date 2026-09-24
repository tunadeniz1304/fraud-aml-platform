"""Logistic stacker: calibrates rule, ML and anomaly scores into one probability.

Fitted on the *validation* split (never on the data the base models saw).
Inputs are logit-transformed so a rule score of 0.99 and 0.999 stay
distinguishable. Coefficients are floored at a small positive value: every
component may only *raise* the risk (monotonicity — a new rule hit or a higher
anomaly can never lower the final score; documented in the model card). The
rule coefficient has a higher floor so fired rules stay material.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

INPUTS = ("rule", "ml", "anomaly")
_EPS = 1e-3


def logit(p: float) -> float:
    p = min(1.0 - _EPS, max(_EPS, p))
    return math.log(p / (1.0 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


@dataclass(frozen=True)
class Stacker:
    coef: tuple[float, float, float]
    intercept: float

    def predict(self, rule: float, ml: float, anomaly: float) -> float:
        z = self.intercept
        for c, x in zip(self.coef, (rule, ml, anomaly), strict=True):
            z += c * logit(x)
        return sigmoid(z)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": list(INPUTS),
            "transform": "logit",
            "coef": list(self.coef),
            "intercept": self.intercept,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stacker:
        c = [float(v) for v in data["coef"]]
        return cls((c[0], c[1], c[2]), float(data["intercept"]))

    @classmethod
    def fit(
        cls,
        rows: Sequence[Sequence[float]],
        labels: Sequence[int],
        *,
        min_coef: float = 0.05,
        min_rule_coef: float = 0.3,
        seed: int = 0,
    ) -> Stacker:
        import numpy as np
        from sklearn.linear_model import LogisticRegression

        x = np.array([[logit(v) for v in row] for row in rows], dtype=float)
        y = np.asarray(labels, dtype=int)
        model = LogisticRegression(C=1.0, max_iter=1000, random_state=seed)
        model.fit(x, y)
        coef = [max(min_coef, float(c)) for c in model.coef_[0]]
        # Rules encode analyst knowledge: keep them materially influential even
        # when the GBM dominates on (synthetic) training data.
        coef[0] = max(min_rule_coef, coef[0])
        # Re-fit the intercept for the (possibly) floored coefficients so the
        # mean predicted probability still matches the base rate.
        z = x @ np.asarray(coef)
        intercept = float(model.intercept_[0])
        target = float(y.mean())
        for _ in range(60):
            p = 1.0 / (1.0 + np.exp(-(z + intercept)))
            grad = float(p.mean()) - target
            if abs(grad) < 1e-7:
                break
            intercept -= grad / max(float((p * (1 - p)).mean()), 1e-6)
        return cls((coef[0], coef[1], coef[2]), intercept)
