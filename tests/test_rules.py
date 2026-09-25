"""Rule DSL: safe expression compiler, rule loading, noisy-OR, backtest, reasons."""

from __future__ import annotations

import pytest

from app.features.definitions import feature_names
from app.scoring.expressions import ExpressionError, compile_expression
from app.scoring.reasons import (
    ReasonCode,
    ml_reasons,
    policy_reason,
    render_template,
    top_reasons,
)
from app.scoring.rules import (
    RuleDef,
    RuleError,
    RuleSet,
    backtest,
    load_rule_file,
    load_ruleset,
    noisy_or,
    ruleset_version,
    strongest_action,
    with_overrides,
)

NAMES = ["a", "b", "amount_ratio", "is_new_payee", "cnt_1h"]


class TestExpressionCompiler:
    @pytest.mark.parametrize(
        ("source", "env", "expected"),
        [
            ("a >= 5", {"a": 6}, True),
            ("a >= 5 and b", {"a": 6, "b": 0}, False),
            ("a > 1 or not b", {"a": 0, "b": 0}, True),
            ("0 <= a < 30", {"a": 12}, True),
            ("0 <= a < 30", {"a": -1}, False),
            ("a * 2 + b - 1 == 5", {"a": 2, "b": 2}, True),
            ("a / b", {"a": 1, "b": 0}, 0.0),
            ("a // b + a % b", {"a": 7, "b": 0}, 0.0),
            ("max(a, b) - min(a, b)", {"a": 3, "b": 9}, 6),
            ("abs(-a) + clamp(b, 0, 1) + log1p(0)", {"a": 2, "b": 5}, 3.0),
            ("a in (1, 2, 3)", {"a": 2}, True),
            ("a not in [1, 2]", {"a": 2}, False),
            ("1 if a else 2", {"a": 0}, 2),
            ("-a + +b", {"a": 1, "b": 3}, 2),
            ("a == 'x'", {"a": "x"}, True),
            ("missing_default == 0", {}, True),
        ],
    )
    def test_allowed_constructs(self, source, env, expected):
        names = [*NAMES, "missing_default"]
        assert compile_expression(source, names)(env) == expected

    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os').system('x')",
            "a.__class__",
            "a[0]",
            "[x for x in a]",
            "lambda: 1",
            "open('f')",
            "a ** 2",
            "a is None",
            "~a",
            "a @ b",
            "a if b",
            "(a := 1)",
            "b'bytes' == a",
            "{'k': 1}",
            "f'{a}'",
            "max(a, key=b)",
        ],
    )
    def test_forbidden_constructs(self, source):
        with pytest.raises(ExpressionError):
            compile_expression(source, NAMES)

    def test_unknown_field_fails_at_compile_time(self):
        with pytest.raises(ExpressionError, match="bilinmeyen alan"):
            compile_expression("amount_ratoi >= 5", NAMES)

    def test_empty_long_and_complex_expressions_rejected(self):
        with pytest.raises(ExpressionError):
            compile_expression("   ", NAMES)
        with pytest.raises(ExpressionError):
            compile_expression("a + " * 200 + "a", NAMES)
        with pytest.raises(ExpressionError):
            compile_expression(" or ".join(["a > 1"] * 60), NAMES)

    def test_evaluate_bool_swallows_type_errors(self):
        expr = compile_expression("a > 1", NAMES)
        assert expr.evaluate_bool({"a": "text"}) is False
        assert expr.names == frozenset({"a"})

    def test_m6_arithmetic_is_numeric_only(self):
        # string/list repetition would allocate unbounded memory on the scoring path
        repeat = compile_expression("a * 100000000 == 'x'", NAMES)
        with pytest.raises(TypeError):
            repeat({"a": "x"})
        assert repeat.evaluate_bool({"a": "x"}) is False
        for source in ("a + b", "a % b", "a // b", "(1, 2) * a"):
            assert (
                compile_expression(f"({source}) == 1", NAMES).evaluate_bool({"a": "ab", "b": 2})
                is False
            )
        assert compile_expression("a * 2 == 4", NAMES).evaluate_bool({"a": 2}) is True

    def test_m6_literals_are_capped(self):
        with pytest.raises(ExpressionError, match="metin sabiti"):
            compile_expression("a == '" + "x" * 101 + "'", NAMES)
        with pytest.raises(ExpressionError, match="sayı sabiti"):
            compile_expression("a * 99999999999999999999 > 1", NAMES)
        with pytest.raises(ExpressionError):
            compile_expression("a ** 99", NAMES)
        assert compile_expression("a >= 1e15 and b == 'TR'", NAMES)({"a": 1e15, "b": "TR"})


class TestRuleSet:
    def test_core_rules_compile_against_feature_registry(self):
        definitions = load_rule_file()
        assert len(definitions) >= 22
        ruleset = RuleSet.from_definitions(definitions)
        assert len(ruleset.rules) == len([d for d in definitions if d.enabled])
        assert ruleset.version.startswith("rs-")
        ids = {d.id for d in definitions}
        assert {"R_NEW_PAYEE_HIGH_AMT", "R_STRUCTURING", "R_CHANNEL_UNKNOWN"} <= ids

    def test_noisy_or_and_hints(self):
        assert noisy_or([]) == 0.0
        assert noisy_or([0.5, 0.5]) == pytest.approx(0.75)
        assert noisy_or([1.5]) == 1.0
        assert strongest_action([None, "STEP_UP", "HOLD", "ALLOW"]) == "HOLD"
        assert strongest_action([]) is None

    def test_evaluate_hits_score_and_reason_text(self):
        ruleset = load_ruleset()
        feats = dict.fromkeys(feature_names(), 0.0)
        feats.update(is_new_payee=1.0, amount_ratio=8.4, purpose_risk=0.9, active_call=1.0)
        result = ruleset.evaluate(feats)
        ids = [h.rule_id for h in result.hits]
        assert "R_NEW_PAYEE_HIGH_AMT" in ids and "R_APP_SCAM_PATTERN" in ids
        assert result.action_hint == "HOLD"
        assert result.score == pytest.approx(noisy_or(h.score for h in result.hits), abs=1e-6)
        text = next(h.text for h in result.hits if h.rule_id == "R_NEW_PAYEE_HIGH_AMT")
        assert "8,4 katı" in text
        assert result.hits == tuple(sorted(result.hits, key=lambda h: -h.score))
        assert ruleset.score_only(feats) == pytest.approx(result.score, abs=1e-6)
        assert result.as_dict()["hits"][0]["code"]

    def test_country_purpose_ip_channel_are_individually_small(self):
        ruleset = load_ruleset()
        base = dict.fromkeys(feature_names(), 0.0)
        for flag in ("is_high_risk_country", "is_anonymized_ip", "ip_country_mismatch"):
            score = ruleset.evaluate({**base, flag: 1.0}).score
            assert 0.0 < score <= 0.3
        assert 0 < ruleset.evaluate({**base, "channel_unknown": 1.0}).score < 0.1
        assert 0 < ruleset.evaluate({**base, "purpose_risk": 0.6}).score < 0.2

    def test_version_changes_with_definition(self):
        d = RuleDef(id="R_X", name="x", when="amount_ratio >= 5", score=0.4)
        v1 = ruleset_version([d])
        assert v1 == ruleset_version([d])
        assert v1 != ruleset_version([RuleDef.from_dict({**d.to_dict(), "score": 0.5})])
        disabled = RuleDef.from_dict({**d.to_dict(), "enabled": False})
        assert ruleset_version([disabled]) != v1

    @pytest.mark.parametrize(
        ("patch", "message"),
        [
            ({"score": 0}, "skor"),
            ({"score": 1.5}, "skor"),
            ({"action_hint": "NUKE"}, "action_hint"),
            ({"severity": "extreme"}, "severity"),
            ({"id": "bad id!"}, "kimliği"),
            ({"when": "no_such_feature > 1"}, "bilinmeyen alan"),
        ],
    )
    def test_invalid_definitions(self, patch, message):
        base = {"id": "R_OK", "name": "ok", "when": "amount_ratio > 1", "score": 0.3}
        with pytest.raises(RuleError, match=message):
            RuleDef.from_dict({**base, **patch}).validate()

    def test_from_dict_requires_fields_and_rejects_duplicates(self):
        with pytest.raises(RuleError):
            RuleDef.from_dict({"id": "R"})
        d = RuleDef(id="R_D", name="d", when="cnt_1h > 1", score=0.2)
        with pytest.raises(RuleError, match="tekrar"):
            RuleSet.from_definitions([d, d])

    def test_backtest_counts_alerts_precision_recall(self):
        d = RuleDef(id="R_B", name="b", when="amount_ratio >= 5", score=0.5)
        rows = [
            ({"amount_ratio": 9}, 1),
            ({"amount_ratio": 7}, 0),
            ({"amount_ratio": 6}, None),
            ({"amount_ratio": 1}, 1),
            ({"amount_ratio": 1}, 0),
        ]
        result = backtest(d, rows, source="test").as_dict()
        assert result["alerts"] == 3 and result["evaluated"] == 5
        assert result["labelled_alerts"] == 2 and result["true_positives"] == 1
        assert result["precision"] == 0.5 and result["recall"] == 0.5
        assert backtest(d, []).as_dict()["precision"] is None

    def test_with_overrides_keeps_version(self):
        d = RuleDef(id="R_O", name="o", when="cnt_1h > 1", score=0.2, version=4)
        draft = with_overrides(d, {"when": "cnt_1h > 5", "score": None})
        assert draft.when == "cnt_1h > 5" and draft.score == 0.2 and draft.version == 4


class TestReasons:
    def test_render_template_turkish_decimal_and_missing(self):
        assert render_template("{x:.1f} kat", {"x": 8.44}) == "8,4 kat"
        assert render_template("{y} yok", {}) == "? yok"
        assert render_template("{b}", {"b": True}) == "evet"
        assert render_template("bozuk {", {}) == "bozuk {"
        assert render_template("{s}", {"s": "metin"}) == "metin"

    def test_ml_reasons_group_positive_contributions(self):
        contrib = {"amount_ratio": 1.2, "amount_zscore": 0.4, "is_new_device": 0.9, "hour": -2.0}
        feats = {"amount_ratio": 8.4, "is_new_device": 1.0, "amount_zscore": 3.0}
        reasons = ml_reasons(contrib, feats)
        codes = [r.code for r in reasons]
        assert codes[0] == "ML_AMOUNT" and "ML_DEVICE_NETWORK" in codes
        assert "ML_TIME" not in codes
        assert "8,40" in reasons[0].text
        assert all(0 < r.weight < 1 for r in reasons)
        assert ml_reasons({"external_x": 0.3}, {})[0].code == "ML_EXTERNAL"

    def test_top_reasons_policy_first_dedup_and_k(self):
        policy = [policy_reason("SANCTIONS_HIT")]
        rules = [ReasonCode(f"R_{i}", "t", "rule", 0.1 * i) for i in range(1, 8)]
        dup = [ReasonCode("R_7", "t2", "rule", 0.01)]
        out = top_reasons(rules, dup, policy, k=4)
        assert out[0].code == "SANCTIONS_HIT"
        assert [r.code for r in out[1:]] == ["R_7", "R_6", "R_5"]
        assert out[1].text == "t"
        assert policy_reason("X", text="özel").as_dict() == {
            "code": "X",
            "text": "özel",
            "source": "policy",
            "weight": 1.0,
        }
