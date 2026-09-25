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
