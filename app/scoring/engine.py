"""Scoring engine — the synchronous, LLM-free hot path (p99 < 50 ms).

``score(tx)``::

    account state gate (BLOKE → BLOCK, unknown → HOLD)
      → features (streaming feature store, point-in-time)
      → external signals (burst, graph, APP, consortium …)
      → rules (safe DSL, noisy-OR, action hints)
      → LightGBM probability (+ TreeSHAP when the decision needs explaining)
      → anomaly (IsolationForest + ECOD percentiles)
      → sanctions / PEP screening (fuzzy)
      → policy (stacker + signals → risk → ALLOW / STEP_UP / HOLD / BLOCK)
      → top reason codes
    [+ shadow scoring with the challenger model, never affecting the decision]

``commit(result)`` records the event in the feature windows; whether the
adaptive profile learns is decided by :func:`app.features.learning.should_learn`
(ALLOW, a passed step-up or an analyst's clean label — poisoning protection).
Events that are not learned at once wait (bounded) for that late feedback.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.core.sanctions import SanctionScreener
from app.features.extractor import CustomerDirectory, Extraction, FeatureExtractor
from app.features.learning import Feedback, should_learn
from app.features.types import TxView
from app.ml.monitoring import DriftMonitor
from app.ml.registry import ModelBundle, ModelRegistry, ModelScore
from app.monitoring import metrics
from app.scoring.policy import PolicyDecision, PolicyEngine, PolicyInput
from app.scoring.reasons import ReasonCode, ml_reasons, policy_reason, top_reasons
from app.scoring.rules import RuleEvaluation, RuleSet, load_ruleset
from app.scoring.signals import BurstSignal, ExternalSignal, SignalResult

if TYPE_CHECKING:
    from app.app_scam.cop import PayeeRegistry
    from app.graph.entity_graph import EntityGraph

logger = logging.getLogger("fraud.scoring")


@dataclass
class ScoreResult:
    transaction_id: str
    customer_id: str
    amount_try: float
    policy: PolicyDecision
    scored: bool
    features: dict[str, float] = field(default_factory=dict)
    rules: RuleEvaluation | None = None
    model: ModelScore | None = None
    signals: dict[str, SignalResult] = field(default_factory=dict)
    sanctions: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[ReasonCode] = field(default_factory=list)
    rule_version: str = ""
    model_version: str = ""
    challenger_version: str | None = None
    challenger_score: float | None = None
    latency_ms: float = 0.0
    extraction: Extraction | None = field(default=None, repr=False)
    flags: dict[str, bool] = field(default_factory=dict)

    @property
    def decision(self) -> str:
        return self.policy.decision

    @property
    def risk_score(self) -> float:
        return self.policy.risk_score

    def components(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {
            "rule": self.rules.score if self.rules else None,
            "ml": round(self.model.ml, 6) if self.model else None,
            "anomaly": round(self.model.anomaly, 6)
            if self.model and self.model.anomaly is not None
            else None,
            "stacked": self.policy.stacked,
        }
        for name, sig in self.signals.items():
            out[name] = sig.score
        return out

    def explanation(self, top: int = 10) -> list[dict[str, float | str]]:
        """Top TreeSHAP contributions (log-odds) for the waterfall view."""
        if self.model is None or not self.model.contributions:
            return []
        items = sorted(self.model.contributions.items(), key=lambda kv: -abs(kv[1]))[:top]
        return [
            {"feature": k, "value": round(v, 5), "x": self.features.get(k, 0.0)} for k, v in items
        ]

    def event_fields(self) -> dict[str, Any]:
        """Fields merged into the ``transaction.analyzed`` event."""
        burst = self.signals.get("burst")
        graph = self.signals.get("graph")
        return {
            "amount_try": self.amount_try,
            "risk_score": self.risk_score,
            "decision": self.decision,
            "decision_legacy": self.policy.legacy,
            "policy": self.policy.as_dict(),
            "components": self.components(),
            "rule_hits": [h.as_dict() for h in self.rules.hits] if self.rules else [],
            "reason_codes": [r.as_dict() for r in self.reasons],
            "risk_explanation": [r.text for r in self.reasons],
            "sanctions": self.sanctions,
            "sanctions_hit": bool(self.sanctions),
            "force_review": bool(self.sanctions) or self.flags.get("unknown_customer", False),
            "case_required": self.policy.case_required,
            "hold_minutes": self.policy.hold_minutes,
            "features": self.features,
            "scored": self.scored,
            "rule_version": self.rule_version,
            "model_version": self.model_version,
            "challenger_version": self.challenger_version,
            "challenger_score": self.challenger_score,
            "latency_ms": round(self.latency_ms, 3),
            "explanation": self.explanation(),
            "microcluster": burst.score if burst else 0.0,
            "mule_score": graph.score if graph else 0.0,
            "mule_signals": [r.text for r in graph.reasons] if graph else [],
            "ring_id": graph.details.get("ring_id") if graph else None,
            "graph": graph.details if graph else None,
            "app": self.signals["app"].details if "app" in self.signals else None,
            "app_warning": self.signals["app"].details.get("warning")
            if "app" in self.signals and self.decision in ("HOLD", "STEP_UP")
            else None,
            "high_risk_country": self.features.get("is_high_risk_country", 0.0) >= 1.0,
            **self.flags,
        }


def build_signals(
    customers: CustomerDirectory,
) -> tuple[EntityGraph | None, PayeeRegistry, list[ExternalSignal]]:
    """Default external signals: burst, entity graph, APP/CoP, online anomaly."""
    from app.app_scam.cop import PayeeRegistry
    from app.app_scam.engine import APPScamSignal
    from app.graph.entity_graph import EntityGraph
    from app.graph.signal import GraphSignal
    from app.profile.online import OnlineAnomalySignal

    settings = get_settings()
    records = customers.records()
    payees = PayeeRegistry.build(records, settings.resolved_payees_path)
    signals: list[ExternalSignal] = [BurstSignal()]
    graph = None
    if settings.graph_enabled:
        graph = EntityGraph()
        for record in records:
            graph.register_customer(
                str(record["customer_id"]),
                str(record.get("name", "")),
                str(record.get("iban") or ""),
            )
        signals.append(GraphSignal(graph))
    signals.append(APPScamSignal(payees))
    if settings.online_anomaly_enabled:
        signals.append(OnlineAnomalySignal())
    if settings.consortium_enabled:
        from app.consortium import ConsortiumHub, ConsortiumSignal

        signals.append(ConsortiumSignal(ConsortiumHub()))
    return graph, payees, signals


class ScoringEngine:
    def __init__(
        self,
        extractor: FeatureExtractor,
        *,
        ruleset: RuleSet,
        policy: PolicyEngine,
        sanctions: SanctionScreener,
        model: ModelBundle | None = None,
        challenger: ModelBundle | None = None,
        signals: Sequence[ExternalSignal] = (),
        graph: EntityGraph | None = None,
        payees: PayeeRegistry | None = None,
    ) -> None:
        self.extractor = extractor
        self.graph = graph
        self.payees = payees
        self.drift: DriftMonitor | None = None
        self.ruleset = ruleset
        self.policy = policy
        self.sanctions = sanctions
        self.model = model
        self.challenger = challenger
        self.signals: list[ExternalSignal] = list(signals)
        #: events not learned at commit time, waiting for step-up / analyst feedback
        self.awaiting_feedback: OrderedDict[str, TxView] = OrderedDict()

    # --- construction ---------------------------------------------------------------
    @classmethod
    def from_settings(
        cls,
        extractor: FeatureExtractor | None = None,
        *,
        ruleset: RuleSet | None = None,
        registry: ModelRegistry | None = None,
        signals: Sequence[ExternalSignal] | None = None,
    ) -> ScoringEngine:
        settings = get_settings()
        if extractor is None:
            extractor = FeatureExtractor(
                customers=CustomerDirectory.from_file(settings.resolved_customers_path)
            )
        model = challenger = None
        if settings.model_enabled:
            registry = registry or ModelRegistry(settings.resolved_models_dir)
            try:
                model = registry.load_role("champion")
                challenger = registry.load_role("challenger")
            except Exception:  # corrupt/missing artefacts must not stop scoring
                logger.exception("[Scoring] model yüklenemedi — yalnız kurallarla skorlanacak")
                model = challenger = None
        if model is None:
            logger.warning("[Scoring] champion model yok — kural + politika ile skorlanıyor")
        graph, payees, default = build_signals(extractor.customers)
        engine = cls(
            extractor,
            ruleset=ruleset or load_ruleset(settings.resolved_rules_path),
            policy=PolicyEngine(model.stacker if model else None),
            sanctions=SanctionScreener().load(),
            model=model,
            challenger=challenger,
            signals=signals if signals is not None else default,
            graph=graph,
            payees=payees,
        )
        engine.set_models(model, challenger)
        return engine

    def set_ruleset(self, ruleset: RuleSet) -> None:
        self.ruleset = ruleset  # atomic reference swap (hot reload)

    def set_models(self, champion: ModelBundle | None, challenger: ModelBundle | None) -> None:
        self.model = champion
        self.challenger = challenger
        self.policy.stacker = champion.stacker if champion else None
        reference = (champion.metadata.get("psi_reference") if champion else None) or {}
        self.drift = DriftMonitor(reference) if reference else None
        for role, bundle in (("champion", champion), ("challenger", challenger)):
            if bundle is not None:
                metrics.MODEL_INFO.labels(role=role, version=bundle.version).set(1)

    @property
    def consortium(self) -> Any:
        for signal in self.signals:
            if signal.name == "consortium":
                return getattr(signal, "hub", None)
        return None

    @property
    def customers(self) -> CustomerDirectory:
        return self.extractor.customers

    # --- screening ------------------------------------------------------------------
    def screen(self, customer_name: str, tx: dict[str, Any]) -> list[dict[str, Any]]:
        """Screen the customer and the beneficiary names (ids are screened only
        when they look like a name, e.g. legacy feeds that put the name there)."""
        names = [customer_name, str(tx.get("beneficiary_name") or "")]
        beneficiary_id = str(tx.get("beneficiary_id") or "")
        if " " in beneficiary_id.strip():
            names.append(beneficiary_id)
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in filter(None, names):
            for hit in self.sanctions.screen(name):
                key = f"{hit.get('id')}|{name}"
                if key not in seen:
                    seen.add(key)
                    hits.append({**hit, "matched_on": name})
        return hits

    # --- hot path -------------------------------------------------------------------
    async def score(self, tx: dict[str, Any], *, account_status: str | None = None) -> ScoreResult:
        started = time.perf_counter()
        settings = get_settings()
        extraction = await self.extractor.extract(tx)
        tx_id, customer_id = str(tx["transaction_id"]), str(tx["customer_id"])
        base: dict[str, Any] = {
            "transaction_id": tx_id,
            "customer_id": customer_id,
            "amount_try": extraction.tx.amount_try,
            "extraction": extraction,
            "rule_version": self.ruleset.version,
            "model_version": self.model.version if self.model else "",
        }
        if account_status == "BLOKE":
            decision = self.policy.decide(PolicyInput(account_blocked=True))
            return ScoreResult(
                **base,
                policy=decision,
                scored=False,
                reasons=[policy_reason("ACCOUNT_BLOCKED")],
                flags={"account_blocked": True},
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        if customer_id not in self.extractor.customers:
            decision = self.policy.decide(PolicyInput(unknown_customer=True))
            return ScoreResult(
                **base,
                policy=decision,
                scored=False,
                reasons=[policy_reason("UNKNOWN_CUSTOMER")],
                flags={"unknown_customer": True},
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        features = dict(extraction.features)
        signal_results: dict[str, SignalResult] = {}
        for signal in self.signals:
            sig = signal.evaluate(tx, extraction, features)
            signal_results[signal.name] = sig
            features.update(sig.features)
        rules = self.ruleset.evaluate(features)
        model_score = self.model.score(features, explain=False) if self.model else None
        customer = self.extractor.customers.get(customer_id)
        sanctions = self.screen(customer.name if customer else "", tx)
        decision = self.policy.decide(
            PolicyInput(
                rule_score=rules.score,
                ml_score=model_score.ml if model_score else None,
                anomaly_score=model_score.anomaly if model_score else None,
                signals={name: r.score for name, r in signal_results.items()},
                action_hint=rules.action_hint,
                sanctions_hit=bool(sanctions),
                cap=self._typology_cap(rules, signal_results, features),
            )
        )
        if (
            self.model is not None
            and model_score is not None
            and (decision.decision != "ALLOW" or decision.risk_score >= settings.explain_min_risk)
        ):
            # TreeSHAP only where an explanation is needed (explanation budget).
            model_score.contributions = self.model.explain(features)
        reasons = self._reasons(rules, model_score, signal_results, decision, features)
        result = ScoreResult(
            **base,
            policy=decision,
            scored=True,
            features=features,
            rules=rules,
            model=model_score,
            signals=signal_results,
            sanctions=sanctions,
            reasons=reasons,
        )
        if self.challenger is not None:
            shadow = self.challenger.score(features, explain=False)
            result.challenger_version = self.challenger.version
            result.challenger_score = round(
                self.challenger.stacker.predict(
                    rules.score, shadow.ml, shadow.anomaly if shadow.anomaly is not None else 0.5
                ),
                6,
            )
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    @staticmethod
    def _typology_cap(
        rules: RuleEvaluation, signals: dict[str, SignalResult], features: dict[str, float]
    ) -> str | None:
        """APP victims and (possibly unwitting) mules: HOLD + warn, not BLOCK —
        unless account-takeover evidence says the customer is not in control."""
        settings = get_settings()
        if any("takeover" in h.tags for h in rules.hits):
            return None
        app = signals.get("app")
        graph = signals.get("graph")
        app_pattern = app is not None and app.score >= settings.app_confirm_threshold
        mule_pattern = graph is not None and (
            graph.details.get("pass_through", 0.0) >= 0.7 or bool(graph.details.get("cycle"))
        )
        # AML (structuring/layering): hold + ŞİB, never a visible block — MASAK
        # tipping-off prohibition (the suspect must not be alerted).
        aml_pattern = (
            any("aml" in h.tags for h in rules.hits) or features.get("near_threshold", 0.0) >= 1.0
        )
        return "HOLD" if app_pattern or mule_pattern or aml_pattern else None

    def _reasons(
        self,
        rules: RuleEvaluation,
        model_score: ModelScore | None,
        signals: dict[str, SignalResult],
        decision: PolicyDecision,
        features: dict[str, float],
    ) -> list[ReasonCode]:
        settings = get_settings()
        policy = [policy_reason(code) for code in decision.overrides if code != "RULE_FLOOR"]
        rule_reasons = [ReasonCode(h.reason_code, h.text, "rule", h.score) for h in rules.hits]
        ml = ml_reasons(model_score.contributions, features) if model_score else []
        anomaly: list[ReasonCode] = []
        if model_score and model_score.anomaly is not None and model_score.anomaly >= 0.995:
            anomaly.append(
                ReasonCode("ANOMALY_HIGH", policy_reason("ANOMALY_HIGH").text, "ml", 0.5)
            )
        signal_reasons = [r for s in signals.values() for r in s.reasons]
        return top_reasons(
            policy, rule_reasons, ml, anomaly, signal_reasons, k=settings.reason_top_k
        )

    async def commit(self, result: ScoreResult, feedback: Feedback | None = None) -> None:
        """Record the event; learn it now only if :func:`should_learn` allows."""
        if result.extraction is None or not result.scored:
            return
        trusted = should_learn(result.decision, feedback)
        await self.extractor.commit(result.extraction, learn_profile=trusted)
        if trusted:
            self._learn_signals(result.transaction_id)
        elif feedback is None:
            self.awaiting_feedback[result.transaction_id] = result.extraction.tx
            limit = get_settings().feedback_pending_max
            while len(self.awaiting_feedback) > limit:
                self.awaiting_feedback.popitem(last=False)

    async def apply_feedback(self, transaction_id: str, feedback: Feedback) -> bool:
        """Late feedback (OTP result, analyst label) for a committed event.

        Returns ``True`` when the profile learned the event (e.g. the new
        device and payee of a passed step-up are now verified).
        """
        tx = self.awaiting_feedback.pop(transaction_id, None)
        if tx is None or not should_learn("", feedback):
            return False
        await self.extractor.learn_event(tx)
        self._learn_signals(transaction_id)
        return True

    def _learn_signals(self, transaction_id: str) -> None:
        for signal in self.signals:
            learn = getattr(signal, "learn", None)
            if learn is not None:
                learn(transaction_id)
