"""ML-path sanity check on the ULB credit-card data set (OpenML 1597).

The 28 features are anonymised PCA components (plus ``Amount``; the
original ``Time`` is absent here), so none of the platform's behavioural features, rules or graph
signals can be computed. This check therefore exercises only the model
machinery — LightGBM, IsolationForest + ECOD with quantile calibration and
the logistic stacker (``rule`` input fixed to 0) — on real card data, with a
time split (first 70 % of the period trains, its last 15 % validates). The
OpenML copy has no ``Time`` column but keeps the source's chronological row
order, so the row index serves as time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.ml import metrics as M
from app.ml.anomaly import ECODScorer, FastIsolationForest, QuantileCalibrator
from app.ml.gbm import GBMModel
from app.ml.stacker import Stacker


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


def evaluate(path: Path, *, seed: int = 42) -> dict[str, Any]:
    from sklearn.ensemble import IsolationForest

    x, y, t, amount, names = load(path)
    cutoff = t.min() + 0.70 * (t.max() - t.min())
    before, after = np.flatnonzero(t <= cutoff), np.flatnonzero(t > cutoff)
    n_valid = int(len(before) * 0.15)
    tr, va, te = before[:-n_valid], before[-n_valid:], after
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
        min_rule_coef=0.0,
        seed=seed,
    )
    ml_te, an_te = gbm.predict(x[te]), anomaly(x[te])
    hybrid = np.asarray([stacker.predict(0.0, m, a) for m, a in zip(ml_te, an_te, strict=True)])
    y_te, a_te = y[te], amount[te]
    out = {
        name: M.evaluate(y_te.tolist(), s.tolist(), a_te.tolist())
        for name, s in (("gbm", ml_te), ("anomaly", an_te), ("hybrid", hybrid))
    }
    # calibration: Brier score + mean predicted vs observed fraud rate
    for name, s in (("gbm", ml_te), ("hybrid", hybrid)):
        out[name]["brier"] = round(float(np.mean((s - y_te) ** 2)), 6)
        out[name]["mean_score"] = round(float(np.mean(s)), 6)
    return {
        "rows": {"train": len(tr), "valid": len(va), "test": len(te)},
        "test_fraud": int(y_te.sum()),
        "test_fraud_rate": round(float(y_te.mean()), 5),
        "results": out,
        "limitation": (
            "Özellikler PCA bileşenleri: kural motoru, feature store ve graf katmanı "
            "uygulanamaz; yalnızca GBM + anomali + kalibrasyon hattı test edilir."
        ),
    }
