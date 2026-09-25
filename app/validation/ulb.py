"""ML-stack-only check on the ULB credit-card data set (OpenML 1597).

The 28 features are anonymised PCA components (plus ``Amount``; the original
``Time`` is absent here). None of the platform's behavioural features, rules,
graph signals or policy can be computed. This check therefore tests **only the
ML stack**: LightGBM, IsolationForest + ECOD with quantile calibration and the
non-negative logistic stacker (``rule`` input fixed to 0). It says nothing
about the rest of the platform.

Split: time-ordered rows 70/15/15, the same protocol as
``app.validation.evaluate``. The OpenML copy has no ``Time`` column but keeps
the source's chronological row order, so the row index serves as time.
Confidence intervals come from the shared stratified bootstrap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.ml import metrics as M
from app.ml.anomaly import ECODScorer, FastIsolationForest, QuantileCalibrator
from app.ml.gbm import GBMModel
from app.ml.stacker import Stacker
from app.validation.evaluate import BOOTSTRAP_ROUNDS, BUDGETS, bootstrap, layer_metrics


def load(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    import pandas as pd

    df = pd.read_csv(path)
    # OpenML 1597 drops the original "Time" column but keeps the chronological
    # row order of the source file, so the row index is the time proxy.
    order = df["Time"].to_numpy() if "Time" in df.columns else np.arange(len(df), dtype=float)
    label_col = "Class" if "Class" in df.columns else df.columns[-1]
    y = df[label_col].astype(str).str.strip("'").astype(int).to_numpy()
    names = [c for c in df.columns if c not in (label_col, "Time")]
    return df[names].to_numpy(dtype=float), y, order, df["Amount"].to_numpy(), names


def evaluate(path: Path, *, seed: int = 42, rounds: int = BOOTSTRAP_ROUNDS) -> dict[str, Any]:
    from sklearn.ensemble import IsolationForest

    x, y, t, amount, names = load(path)
    order = np.argsort(t, kind="stable")
    n = len(order)
    tr, va, te = (
        order[: int(n * 0.70)],
        order[int(n * 0.70) : int(n * 0.85)],
        order[int(n * 0.85) :],
    )
    gbm = GBMModel.train(x[tr], y[tr], x[va], y[va], names, seed=seed, num_boost_round=300)
    clean = x[tr][y[tr] == 0]
    rng = np.random.default_rng(seed)
    iso = IsolationForest(n_estimators=100, max_samples=256, random_state=seed).fit(clean)
    iforest = FastIsolationForest.from_sklearn(iso)
    ecod = ECODScorer.fit(clean[np.sort(rng.choice(len(clean), 3000, replace=False))])
    va_clean = x[va][y[va] == 0]
    cal_if = QuantileCalibrator.fit([iforest.anomaly(r) for r in va_clean.tolist()])
    cal_ec = QuantileCalibrator.fit([ecod.score(r) for r in va_clean.tolist()])

    def anomaly(rows: np.ndarray) -> np.ndarray:
        return np.asarray(
            [0.5 * (cal_if(iforest.anomaly(r)) + cal_ec(ecod.score(r))) for r in rows.tolist()]
        )

    ml_va, an_va = gbm.predict(x[va]), anomaly(x[va])
    stacker = Stacker.fit(
        [(0.0, m, a) for m, a in zip(ml_va, an_va, strict=True)],
        y[va].tolist(),
        seed=seed,
    )
    ml_te, an_te = gbm.predict(x[te]), anomaly(x[te])
    hybrid = np.asarray([stacker.predict(0.0, m, a) for m, a in zip(ml_te, an_te, strict=True)])
    y_te, a_te = y[te], amount[te]
    scores = {"gbm": ml_te, "anomaly": an_te, "hybrid": hybrid}
    ci = bootstrap(y_te, scores, rounds=rounds, chain=(), baseline="gbm", versus=("hybrid",))
    out: dict[str, Any] = {}
    for name, s in scores.items():
        m = layer_metrics(y_te, s, a_te)
        m["pr_auc_ci95"] = ci[name]["pr_auc"]
        m["roc_auc_ci95"] = ci[name]["roc_auc"]
        for b in BUDGETS:
            m[f"budget_{b:g}"]["recall_ci95"] = ci[name][f"recall_{b:g}"]
        out[name] = m
    for name in ("gbm", "hybrid"):
        out[name]["calibration"] = M.calibration(y_te, scores[name])
    return {
        "rows": {"train": len(tr), "valid": len(va), "test": len(te)},
        "test_fraud": int(y_te.sum()),
        "test_fraud_rate": round(float(y_te.mean()), 5),
        "results": out,
        "hybrid_minus_gbm_pr_auc_ci95": ci["vs_baseline_pr_auc"]["hybrid"],
        "stacker": stacker.to_dict(),
        "bootstrap": {"rounds": rounds, "method": "stratified percentile (pos/neg ayrı)"},
        "limitation": (
            "Yalnızca ML yığını test edilir: özellikler PCA bileşenleri olduğu için kural "
            "motoru, feature store, graf katmanı ve politika uygulanamaz."
        ),
    }
