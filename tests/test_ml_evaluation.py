"""ML evaluation hygiene: non-oracle step-up outcomes, PaySim time offsets,
stacker calibration and the evaluation layers."""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from pathlib import Path

from app.features.learning import DEFAULT_STEP_UP_MODEL, StepUpOutcomeModel

FIXTURES = Path(__file__).parent / "fixtures"


def _tx(i: int, label: int, typology: str = "normal", **extra: object) -> dict:
    return {"transaction_id": f"T{i}", "label": label, "typology": typology, **extra}


def test_step_up_outcome_is_not_an_oracle() -> None:
    model = StepUpOutcomeModel(fraud_pass_rate=0.3, genuine_fail_rate=0.05, seed=1)
    genuine = Counter(model.feedback("STEP_UP", _tx(i, 0)) for i in range(4000))
    fraud = Counter(model.feedback("STEP_UP", _tx(i, 1, "ato")) for i in range(4000))
    assert 0.03 < genuine["step_up_failed"] / 4000 < 0.07
    assert 0.25 < fraud["step_up_passed"] / 4000 < 0.35
    # an APP-scam victim authorises the payment and passes the challenge
    app = Counter(model.feedback("STEP_UP", _tx(i, 1, "app")) for i in range(1000))
    assert app["step_up_passed"] > 850


def test_step_up_outcome_only_for_step_up_and_deterministic() -> None:
    tx = _tx(7, 1, "ato")
    assert DEFAULT_STEP_UP_MODEL.feedback("ALLOW", tx) is None
    assert DEFAULT_STEP_UP_MODEL.feedback("HOLD", tx) is None
    first = DEFAULT_STEP_UP_MODEL.feedback("STEP_UP", tx)
    assert first == DEFAULT_STEP_UP_MODEL.feedback("STEP_UP", dict(tx))


def test_step_up_outcome_uses_truth_not_noisy_label() -> None:
    model = StepUpOutcomeModel(fraud_pass_rate=0.0, genuine_fail_rate=0.0)
    # a missed fraud (label flipped to 0) is still a fraudster at the OTP prompt
    missed = _tx(1, 0, "ato", label_noise=True)
    assert model.feedback("STEP_UP", missed) == "step_up_failed"
    assert model.feedback("STEP_UP", _tx(2, 0)) == "step_up_passed"


def test_paysim_offsets_keep_order_and_stay_inside_the_hour() -> None:
    from datetime import datetime

    from app.datasets import paysim_adapter as ps

    txs = ps.load_transactions(FIXTURES / "paysim_sample.csv", limit=2000)
    base = datetime.fromisoformat(txs[0]["ts"]).replace(minute=0, second=0, microsecond=0)
    for prev, cur in pairwise(txs):
        assert prev["ts"] <= cur["ts"] or prev["step"] < cur["step"]
    for tx in txs:
        ts = datetime.fromisoformat(tx["ts"])
        hours = (ts - base).total_seconds() // 3600
        assert hours == tx["step"] - txs[0]["step"]


def test_split_bounds_give_row_shares_70_15_15() -> None:
    import numpy as np

    from app.validation.evaluate import split_bounds

    ts = np.repeat(np.arange(200, dtype=float), 5)  # ties: 5 rows per timestamp
    t1, t2 = split_bounds(ts)
    train, valid = (ts <= t1).mean(), ((ts > t1) & (ts <= t2)).mean()
    assert abs(train - 0.70) <= 0.01 and abs(valid - 0.15) <= 0.01


def test_rank_metrics_match_sklearn_and_bootstrap_is_stratified() -> None:
    import numpy as np
    from sklearn.metrics import average_precision_score, roc_auc_score

    from app.validation.evaluate import _rank_metrics, bootstrap

    rng = np.random.default_rng(0)
    y = (rng.random(2000) < 0.02).astype(int)
    s = np.round(rng.random(2000) + y * 0.5, 2)  # ties exercise the step handling
    ap, auc, _ = _rank_metrics(y, s, [20])
    assert abs(ap - average_precision_score(y, s)) < 1e-9
    assert abs(auc - roc_auc_score(y, s)) < 1e-9
    ci = bootstrap(y, {"gbm": s, "full": s + 0.0}, rounds=50, chain=(), versus=("full",))
    lo, hi = ci["gbm"]["pr_auc"]
    assert lo <= ap <= hi
    assert ci["vs_baseline_pr_auc"]["full"] == [0.0, 0.0]


def test_calibration_metrics() -> None:
    import numpy as np

    from app.ml import metrics as M

    y = np.array([0, 0, 1, 1])
    perfect = M.calibration(y, y.astype(float))
    assert perfect["brier"] == 0 and perfect["ece_10bin"] == 0
    flat = M.calibration(y, np.full(4, 0.5))
    assert flat["brier"] == 0.25 and flat["ece_10bin"] == 0  # calibrated but useless
    over = M.calibration(np.zeros(10), np.full(10, 0.3))
    assert abs(over["ece_10bin"] - 0.3) < 1e-9
