"""Policy layer: thresholds, stacking, overrides, legacy mapping."""

from __future__ import annotations

import pytest

from app.ml.stacker import Stacker, logit, sigmoid
from app.scoring.policy import (
    ACCOUNT_TARGET,
    LEGACY,
    PolicyEngine,
    PolicyError,
    PolicyInput,
    Thresholds,
)


@pytest.fixture()
def engine() -> PolicyEngine:
    return PolicyEngine(
        Stacker((0.3, 1.5, 0.05), 3.0),
        Thresholds(0.35, 0.60, 0.85),
        {"graph": 0.5, "app": 0.5},
    )


class TestThresholds:
    @pytest.mark.parametrize(
        ("risk", "action"),
        [(0.0, "ALLOW"), (0.35, "STEP_UP"), (0.6, "HOLD"), (0.84, "HOLD"), (0.85, "BLOCK")],
    )
    def test_mapping(self, risk, action):
        assert Thresholds(0.35, 0.6, 0.85).action(risk) == action

    @pytest.mark.parametrize("values", [(0.6, 0.35, 0.85), (0.0, 0.5, 0.9), (0.2, 0.5, 1.2)])
    def test_invalid(self, values):
        with pytest.raises(PolicyError):
            Thresholds(*values)

    def test_runtime_change(self, engine):
        engine.set_thresholds(0.2, 0.4, 0.6)
        assert engine.decide_from_risk(0.5).decision == "HOLD"
        assert engine.thresholds.as_dict() == {"step_up": 0.2, "hold": 0.4, "block": 0.6}


class TestDecisions:
    def test_low_risk_allows(self, engine):
        d = engine.decide(PolicyInput(rule_score=0.0, ml_score=0.001, anomaly_score=0.5))
        assert d.decision == "ALLOW" and d.legacy == "GECTI" and not d.case_required
        assert d.hold_minutes is None

    def test_high_ml_blocks(self, engine):
        d = engine.decide(PolicyInput(rule_score=0.5, ml_score=0.99, anomaly_score=0.99))
        assert d.decision == "BLOCK" and d.risk_score >= 0.85 and d.case_required

    def test_signals_raise_risk_monotonically(self, engine):
        base = engine.decide(PolicyInput(ml_score=0.01, anomaly_score=0.5))
        more = engine.decide(PolicyInput(ml_score=0.01, anomaly_score=0.5, signals={"graph": 1}))
        unknown = engine.decide(PolicyInput(ml_score=0.01, anomaly_score=0.5, signals={"x": 1}))
        assert more.risk_score > base.risk_score
        assert unknown.risk_score == base.risk_score

    def test_rule_hint_is_a_floor(self, engine):
        d = engine.decide(
            PolicyInput(ml_score=0.001, anomaly_score=0.1, action_hint="HOLD", amount_try=60_000)
        )
        assert d.base_decision == "ALLOW" and d.decision == "HOLD"
        assert "RULE_FLOOR" in d.overrides and d.hold_minutes and d.case_required

    def test_low_risk_low_amount_hold_floor_is_softened_to_step_up(self, engine):
        """Audit A4: a thin-history transfer (risk 0.29, 5 096 TRY) must not freeze
        the account — the rule's HOLD floor becomes a step-up challenge."""
        d = engine.decide(
            PolicyInput(ml_score=0.001, anomaly_score=0.1, action_hint="HOLD", amount_try=5_096)
        )
        assert d.decision == "STEP_UP" and not d.case_required
        assert "RULE_FLOOR_SOFTENED" in d.overrides

    def test_sanctions_force_hold_and_case(self, engine):
        d = engine.decide(PolicyInput(ml_score=0.001, sanctions_hit=True))
        assert d.decision == "HOLD" and d.case_required and "SANCTIONS_HIT" in d.overrides
        blocked = engine.decide(PolicyInput(ml_score=0.999, rule_score=0.9, sanctions_hit=True))
        assert blocked.decision == "BLOCK"

    def test_blocked_and_unknown_overrides(self, engine):
        assert engine.decide(PolicyInput(account_blocked=True)).decision == "BLOCK"
        unknown = engine.decide(PolicyInput(unknown_customer=True))
        assert unknown.decision == "HOLD" and unknown.overrides == ("UNKNOWN_CUSTOMER",)
        assert unknown.risk_score < 1.0

    def test_rules_only_fallback_without_model(self):
        engine = PolicyEngine(None, Thresholds(0.35, 0.6, 0.85), {})
        assert engine.decide(PolicyInput(rule_score=0.7)).decision == "HOLD"
        assert engine.stack(0.4, 0.9, 0.9) == 0.4

    def test_decide_from_risk(self, engine):
        assert engine.decide_from_risk(0.6).decision == "HOLD"
        forced = engine.decide_from_risk(0.1, force_review=True)
        assert forced.decision == "HOLD" and forced.overrides == ("FORCED_REVIEW",)
        assert engine.decide_from_risk(0.1, account_blocked=True).decision == "BLOCK"
        assert engine.decide_from_risk(0.9).as_dict()["decision"] == "BLOCK"

    def test_legacy_and_account_mapping(self):
        assert LEGACY == {
            "ALLOW": "GECTI",
            "STEP_UP": "GECTI",
            "HOLD": "INCELENIYOR",
            "BLOCK": "BLOKE",
        }
        assert ACCOUNT_TARGET == {"HOLD": "INCELENIYOR", "BLOCK": "BLOKE"}


class TestStacker:
    def test_logit_sigmoid_roundtrip(self):
        assert sigmoid(logit(0.3)) == pytest.approx(0.3)
        assert sigmoid(-800) == pytest.approx(0.0) and sigmoid(800) == pytest.approx(1.0)

    def test_fit_is_monotone_and_calibrated(self):
        import random

        rng = random.Random(0)
        rows, labels = [], []
        for _ in range(3000):
            y = int(rng.random() < 0.05)
            ml = min(0.999, max(0.001, rng.gauss(0.8 if y else 0.05, 0.15)))
            rule = 0.6 if y and rng.random() < 0.5 else 0.0
            anomaly = rng.random()  # pure noise -> never a negative coefficient
            rows.append((rule, ml, anomaly))
            labels.append(y)
        stacker = Stacker.fit(rows, labels)
        assert all(c >= 0.0 for c in stacker.coef)
        assert stacker.coef[0] > 0.05 and stacker.coef[1] > 0.5
        mean = sum(stacker.predict(*r) for r in rows) / len(rows)
        assert mean == pytest.approx(sum(labels) / len(labels), abs=0.01)
        assert stacker.predict(0.6, 0.5, 0.5) > stacker.predict(0.0, 0.5, 0.5)
        fit = stacker.to_dict()["fit"]
        assert set(fit["ablation"]["variants"]) == {
            "floored_legacy",
            "unconstrained",
            "nonneg",
            "nonneg_no_anomaly",
        }

    def test_noise_input_is_not_forced_in(self):
        """No coefficient floor: an input with no signal gets (close to) zero
        weight, or is dropped by the validation ablation."""
        import random

        rng = random.Random(1)
        rows, labels = [], []
        for _ in range(4000):
            y = int(rng.random() < 0.05)
            ml = min(0.999, max(0.001, rng.gauss(0.7 if y else 0.1, 0.15)))
            rows.append((0.0, ml, rng.random()))
            labels.append(y)
        stacker = Stacker.fit(rows, labels)
        assert stacker.coef[2] < 0.05
        # a rule input that is constant carries no information either
        assert stacker.coef[0] < 0.05

    def test_negative_evidence_is_clipped_to_zero(self):
        import random

        rng = random.Random(2)
        rows, labels = [], []
        for _ in range(3000):
            y = int(rng.random() < 0.1)
            ml = min(0.999, max(0.001, rng.gauss(0.7 if y else 0.1, 0.15)))
            anomaly = min(0.999, max(0.001, rng.gauss(0.2 if y else 0.6, 0.1)))  # inverted
            rows.append((0.0, ml, anomaly))
            labels.append(y)
        stacker = Stacker.fit(rows, labels, select_inputs=False)
        assert stacker.coef[2] == pytest.approx(0.0, abs=1e-6)
        assert Stacker.from_dict(stacker.to_dict()) == stacker
