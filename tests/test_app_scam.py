"""APP scam engine, Confirmation of Payee and the online anomaly profile (P1.1, P1.3)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.app_scam.cop import (
    CLOSE,
    EXACT,
    NO_MATCH,
    UNKNOWN,
    PayeeRecord,
    PayeeRegistry,
    mask_name,
)
from app.app_scam.engine import WARNINGS, APPScamSignal
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.profile.online import OnlineAnomalySignal, behaviour_vector

IBAN = "TR330006100519786457841326"


@pytest.fixture()
def registry() -> PayeeRegistry:
    return PayeeRegistry([PayeeRecord(IBAN, "Ayşe Yılmaz", date(2026, 9, 1), "CUST-9")])


class TestCoP:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("Ayşe Yılmaz", EXACT),
            ("AYSE YILMAZ", EXACT),
            ("Ayşe Yılmazz", EXACT),
            ("Ayşe Yıldız", CLOSE),
            ("Emniyet Güvenli Hesap", NO_MATCH),
        ],
    )
    def test_results(self, registry, typed, expected):
        result = registry.confirm(IBAN, typed)
        assert result.result == expected
        if expected in (CLOSE, NO_MATCH):
            assert result.registered_name_masked == "A*** Y*****"

    def test_unknown_and_masking(self, registry):
        assert registry.confirm("TR000000000000000000000000", "X").result == UNKNOWN
        assert registry.confirm(IBAN, "").result == UNKNOWN
        assert registry.confirm(None, "x").result == UNKNOWN
        assert registry.confirm(IBAN, "!!!").result == UNKNOWN
        assert mask_name("Ali Veli Can") == "A** V*** C**"
        assert registry.confirm(f" {IBAN.lower()} ", "Ayşe Yılmaz").as_dict()["result"] == EXACT

    def test_build_from_customers_and_file(self, tmp_path):
        path = tmp_path / "payees.json"
        path.write_text(
            json.dumps([{"iban": "TR11", "name": "Mule Hesap", "account_opened": "2026-09-10"}]),
            encoding="utf-8",
        )
        reg = PayeeRegistry.build(
            [{"customer_id": "C1", "name": "Can Kaya", "iban": "TR22"}, {"customer_id": "C2"}],
            path,
        )
        assert len(reg) == 2
        assert reg.lookup("TR11").account_opened == date(2026, 9, 10)  # type: ignore[union-attr]
        assert reg.lookup("TR22").owner_customer_id == "C1"  # type: ignore[union-attr]
        assert len(PayeeRegistry.build([], tmp_path / "missing.json")) == 0


async def _extraction(tx: dict, customers: list[dict]):
    extractor = FeatureExtractor(customers=CustomerDirectory(customers))
    return await extractor.extract(tx)


ELDER = {
    "customer_id": "CUST-E",
    "name": "Fatma Demir",
    "avg_amount": 2000,
    "birth_year": 1948,
    "known_device_ids": ["DEV-E"],
    "known_locations": ["İzmir"],
    "typical_hours": list(range(9, 19)),
}
APP_TX = {
    "transaction_id": "APP-1",
    "ts": "2026-09-20T14:00:00",
    "customer_id": "CUST-E",
    "amount": 30_000,
    "currency": "TRY",
    "device_id": "DEV-E",
    "location": "İzmir",
    "country": "TR",
    "purpose": "Polis talimatı ile güvenli hesaba aktarım",
    "beneficiary_iban": IBAN,
    "beneficiary_name": "Emniyet Güvenli Hesap",
    "session": {"active_call": True, "session_duration_s": 2000, "login_to_transfer_s": 1500},
}


class TestAPPSignal:
    async def test_classic_app_pattern(self, registry):
        extraction = await _extraction(APP_TX, [ELDER])
        signal = APPScamSignal(registry)
        result = signal.evaluate(APP_TX, extraction, extraction.features)
        assert result.score >= 0.9
        details = result.details
        assert details["cop"]["result"] == NO_MATCH and details["confirmation_required"]
        assert details["warning"] == WARNINGS["authority"]
        assert details["payee_account_age_d"] == 19.0
        assert {"APP_ACTIVE_CALL", "APP_COP_NO_MATCH", "APP_YOUNG_PAYEE", "APP_VULNERABLE"} <= set(
            details["patterns"]
        )
        assert result.features["cop_no_match"] == 1.0 and result.reasons

    async def test_benign_payment_scores_low(self, registry):
        tx = {
            **APP_TX,
            "amount": 1500,
            "purpose": "Kira",
            "beneficiary_name": "Ayşe Yılmaz",
            "session": {},
        }
        extraction = await _extraction(tx, [ELDER])
        result = APPScamSignal(registry).evaluate(tx, extraction, extraction.features)
        assert result.score < 0.5 and result.details["warning"] is None and not result.reasons

    @pytest.mark.parametrize(
        ("patch", "warning"),
        [
            ({"purpose": "Kripto yatırım fırsatı"}, "investment"),
            ({"purpose": "Ödeme", "session": {"remote_access_tool": True}}, "remote"),
            ({"purpose": "Ödeme", "session": {"active_call": True}}, "call"),
            ({"purpose": "Ödeme", "session": {}}, "cop"),
        ],
    )
    async def test_dynamic_warnings(self, registry, patch, warning):
        tx = {**APP_TX, **patch}
        extraction = await _extraction(tx, [ELDER])
        signal = APPScamSignal(registry)
        assert (
            signal._warning(extraction.features, signal._cop(tx), tx["purpose"])
            == WARNINGS[warning]
        )

    async def test_default_warning_and_known_payee_damping(self):
        tx = {**APP_TX, "purpose": "Ödeme", "session": {}, "beneficiary_iban": "TR99"}
        extraction = await _extraction(tx, [ELDER])
        signal = APPScamSignal(PayeeRegistry())
        assert signal._warning(extraction.features, signal._cop(tx), "Ödeme") == WARNINGS["default"]
        feats = {**extraction.features, "is_new_payee": 0.0}
        assert signal.evaluate(tx, extraction, feats).score < 0.3


class TestOnlineAnomaly:
    async def test_abstains_until_ready_then_flags_outliers(self):
        signal = OnlineAnomalySignal(window=40, n_trees=10, height=6)
        extraction = await _extraction(APP_TX, [ELDER])
        normal = {
            **extraction.features,
            "amount_zscore": 0.1,
            "cnt_1h": 1.0,
            "hour": 14.0,
            "is_new_device": 0.0,
            "is_new_payee": 0.0,
            "typing_deviation": 0.2,
        }
        assert signal.evaluate(APP_TX, extraction, normal).score == 0.0
        assert not signal.learn("never-scored")
        for i in range(120):
            tx = {**APP_TX, "transaction_id": f"N-{i}"}
            ex = await _extraction(tx, [ELDER])
            jitter = {**normal, "amount_zscore": 0.1 + (i % 5) * 0.05, "hour": 13.0 + i % 3}
            signal.evaluate(tx, ex, jitter)
            assert signal.learn(f"N-{i}")
        assert signal.ready and signal.learned == 120
        inlier = signal.evaluate({**APP_TX, "transaction_id": "IN"}, extraction, normal).score
        weird = {
            **normal,
            "amount_zscore": 6.0,
            "cnt_1h": 20.0,
            "hour": 3.0,
            "is_new_device": 1.0,
            "is_new_payee": 1.0,
            "typing_deviation": 8.0,
            "device_customers_7d": 5.0,
        }
        outlier = signal.evaluate({**APP_TX, "transaction_id": "OUT"}, extraction, weird)
        assert outlier.score > inlier
        assert outlier.features["online_anomaly"] == outlier.score

    def test_behaviour_vector_is_bounded(self):
        vec = behaviour_vector({"amount_zscore": 100, "cnt_1h": 1e6, "login_to_transfer_s": 1e9})
        assert all(0.0 <= v <= 1.0 for v in vec.values())
        assert behaviour_vector({})["login"] == 0.5
