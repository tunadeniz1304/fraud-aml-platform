"""Scoring engine: hybrid hot path, overrides, shadow scoring, latency budget."""

from __future__ import annotations

import shutil
import statistics

import pytest

from app.config import BASE_DIR, get_settings
from app.core.event_bus import EventBus
from app.features.extractor import CustomerDirectory, FeatureExtractor
from app.ml.registry import ModelRegistry
from app.scoring.engine import ScoringEngine
from app.scoring.rules import RuleDef, RuleSet
from app.synthetic.generator import SyntheticConfig, generate, strip_labels

CLEAN = {
    "transaction_id": "TX-E1",
    "ts": "2026-09-22T14:30:00",
    "customer_id": "CUST-0001",
    "amount": 2000,
    "currency": "TRY",
    "device_id": "DEV-9F2A-11",
    "location": "İstanbul",
    "country": "TR",
    "purpose": "Market",
}
ATO = {
    "transaction_id": "TX-B1",
    "ts": "2026-09-22T02:15:00",
    "customer_id": "CUST-0002",
    "amount": 90000,
    "currency": "EUR",
    "device_id": "DEV-33CC-10",
    "location": "Berlin",
    "country": "DE",
    "purpose": "Investment",
}


@pytest.fixture()
def engine() -> ScoringEngine:
    return ScoringEngine.from_settings()


class TestHybridScoring:
    async def test_clean_transfer_allowed_with_full_components(self, engine):
        result = await engine.score(CLEAN)
        assert result.decision == "ALLOW" and result.scored
        comps = result.components()
        assert set(comps) >= {"rule", "ml", "anomaly", "stacked", "burst"}
        assert result.model_version == "fraud_gbm_v1"
        assert result.rule_version.startswith("rs-")
        fields = result.event_fields()
        assert fields["decision_legacy"] == "GECTI" and fields["latency_ms"] > 0

    async def test_account_takeover_blocks_with_reasons(self, engine):
        result = await engine.score(ATO)
        assert result.decision == "BLOCK" and result.risk_score >= 0.85
        assert 3 <= len(result.reasons) <= get_settings().reason_top_k
        codes = {r.code for r in result.reasons}
        assert codes & {"R_NEW_DEVICE_HIGH_AMT", "R_ATO_TAKEOVER", "R_EXTREME_AMOUNT"}
        assert any(r.source == "ml" for r in result.reasons)
        assert all(r.text for r in result.reasons)

    async def test_profile_learns_only_from_allow(self, engine):
        store = engine.extractor.store
        allowed = await engine.score(CLEAN)
        await engine.commit(allowed)
        blocked = await engine.score({**ATO, "transaction_id": "TX-B2"})
        await engine.commit(blocked)
        profiles = store.profiles  # type: ignore[attr-defined]
        assert profiles["CUST-0001"].n > profiles["CUST-0002"].n

    async def test_blocked_and_unknown_are_not_scored(self, engine):
        blocked = await engine.score(CLEAN, account_status="BLOKE")
        assert blocked.decision == "BLOCK" and not blocked.scored
        assert blocked.reasons[0].code == "ACCOUNT_BLOCKED"
        unknown = await engine.score({**CLEAN, "customer_id": "CUST-X"})
        assert unknown.decision == "HOLD" and unknown.flags == {"unknown_customer": True}
        await engine.commit(unknown)  # no-op for unscored events
        assert engine.extractor.store.size()["customers"] == 0  # type: ignore[attr-defined]

    async def test_fuzzy_sanctions_hit_forces_hold_and_case(self, engine):
        result = await engine.score(
            {**CLEAN, "transaction_id": "TX-S", "beneficiary_name": "Viktor Melnikof"}
        )
        assert result.decision == "HOLD" and result.policy.case_required
        assert result.sanctions[0]["match_type"] == "fuzzy"
        assert result.reasons[0].code == "SANCTIONS_HIT"
        by_id = await engine.score(
            {**CLEAN, "transaction_id": "TX-S2", "beneficiary_id": "Olga Fedorova"}
        )
        assert by_id.sanctions and by_id.decision == "HOLD"

    async def test_rules_only_fallback_and_hot_reload(self, monkeypatch):
        monkeypatch.setenv("MODEL_ENABLED", "false")
        get_settings.cache_clear()
        try:
            engine = ScoringEngine.from_settings()
        finally:
            monkeypatch.delenv("MODEL_ENABLED")
            get_settings.cache_clear()
        assert engine.model is None and engine.policy.stacker is None
        result = await engine.score(ATO)
        assert result.components()["ml"] is None
        # rules-only stack; external signals (graph/APP/online) may still add risk
        assert result.policy.stacked == result.rules.score  # type: ignore[union-attr]
        assert result.risk_score >= result.policy.stacked
        custom = RuleSet.from_definitions(
            [RuleDef(id="R_ALL", name="all", when="amount_try > 0", score=0.9, action_hint="BLOCK")]
        )
        engine.set_ruleset(custom)
        again = await engine.score({**CLEAN, "transaction_id": "TX-R"})
        assert again.decision == "BLOCK" and again.rule_version == custom.version

    async def test_challenger_shadow_scoring(self, tmp_path):
        src = BASE_DIR / "models" / "fraud_gbm_v1"
        for version in ("champ", "chall"):
            shutil.copytree(src, tmp_path / version)
        registry = ModelRegistry(tmp_path)
        registry.register("champ", metrics={}, status="champion")
        registry.register("chall", metrics={}, status="challenger")
        engine = ScoringEngine.from_settings(registry=registry)
        result = await engine.score(ATO)
        assert result.challenger_version == "chall"
        assert result.challenger_score == pytest.approx(result.policy.stacked, abs=1e-5)
        engine.set_models(None, None)
        assert engine.policy.stacker is None and engine.challenger is None

    async def test_corrupt_model_falls_back_to_rules(self, tmp_path):
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "model.txt").write_text("not a model", encoding="utf-8")
        (tmp_path / "registry.json").write_text(
            '{"champion": "bad", "challenger": null, "models": {"bad": {"status": "champion"}}}',
            encoding="utf-8",
        )
        engine = ScoringEngine.from_settings(registry=ModelRegistry(tmp_path))
        assert engine.model is None
        assert (await engine.score(CLEAN)).decision == "ALLOW"


class TestLatencyBudget:
    async def test_p99_under_50ms_for_1000_transactions(self):
        data = generate(SyntheticConfig(seed=3, customers=60, days=12, ato_attacks=5, app_scams=5))
        extractor = FeatureExtractor(customers=CustomerDirectory(data.customers))
        engine = ScoringEngine.from_settings(extractor)
        latencies: list[float] = []
        normal = flagged_normal = fraud = caught = 0
        for tx in data.transactions[:1000]:
            result = await engine.score(strip_labels(tx))
            await engine.commit(result)
            latencies.append(result.latency_ms)
            if tx["label"]:
                fraud += 1
                caught += result.decision != "ALLOW"
            else:
                normal += 1
                flagged_normal += result.decision != "ALLOW"
        assert len(latencies) == 1000
        latencies.sort()
        p50 = statistics.median(latencies)
        p99 = latencies[int(0.99 * len(latencies)) - 1]
        print(f"senkron skor gecikmesi: p50={p50:.2f} ms p99={p99:.2f} ms (n=1000)")
        assert p99 < 50.0
        assert flagged_normal / normal < 0.05  # cold-start false-positive rate
        assert caught / fraud > 0.8


class TestAnalystIntegration:
    async def test_analyzed_event_carries_policy_payload(self):
        from app.agents.action_agent import ActionAgent
        from app.agents.context_analyst import ContextAnalyst
        from app.agents.transaction_monitor import TransactionMonitor

        bus = EventBus()
        TransactionMonitor(bus)
        analyst = ContextAnalyst(bus)
        action = ActionAgent(bus)
        decided: list[dict] = []
        bus.subscribe(ActionAgent.DECIDED, decided.append)
        await bus.publish(TransactionMonitor.CREATED, dict(ATO))
        event = analyst.analyzed[-1]
        assert event["decision"] == "BLOCK" and event["reason_codes"]
        assert event["components"]["ml"] is not None and event["rule_hits"]
        assert action.blocked[-1]["decision"] == "BLOCK"
        assert decided[-1]["decision_legacy"] == "BLOKE"
        assert "Otonom bloke" in decided[-1]["decision_reason"]
