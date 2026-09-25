"""Policy layer (P0.5): one calibrated risk score -> graduated action.

``risk = 1 - (1 - stacker(rule, ml, anomaly)) · Π(1 - w_i · s_i)``

where ``s_i`` are external signals (graph/mule, burst, APP, consortium) with
configured weights ``w_i``. The risk is mapped onto four actions:

========  =====================================================  ============
action    meaning                                                account
========  =====================================================  ============
ALLOW     let it through                                         unchanged
STEP_UP   ask for OTP / biometric confirmation (simulated)       unchanged
HOLD      hold the payment (cooling-off) until an analyst acts   INCELENIYOR
BLOCK     reject and block the account                           BLOKE
========  =====================================================  ============

Deterministic overrides (never learned, never LLM-driven):

* account already ``BLOKE`` → BLOCK before scoring;
* sanctions/PEP hit → at least HOLD + mandatory case;
* unknown customer → HOLD (human review);
* a fired rule's ``action_hint`` is a floor;
* a typology cap: an APP scam victim or a (possibly unwitting) mule gets a
  HOLD with cooling-off and a warning instead of a hard BLOCK, unless account
  takeover signals are present.

Thresholds come from configuration and can be changed at runtime (admin API).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.ml.stacker import Stacker
from app.scoring.rules import ACTIONS, noisy_or, strongest_action

LEGACY = {"ALLOW": "GECTI", "STEP_UP": "GECTI", "HOLD": "INCELENIYOR", "BLOCK": "BLOKE"}
ACCOUNT_TARGET = {"HOLD": "INCELENIYOR", "BLOCK": "BLOKE"}


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Thresholds:
    step_up: float
    hold: float
    block: float

    def __post_init__(self) -> None:
        if not 0.0 < self.step_up < self.hold < self.block <= 1.0:
            raise PolicyError("eşikler 0 < step_up < hold < block <= 1 olmalı")

    def action(self, risk: float) -> str:
        if risk >= self.block:
            return "BLOCK"
        if risk >= self.hold:
            return "HOLD"
        if risk >= self.step_up:
            return "STEP_UP"
        return "ALLOW"

    def as_dict(self) -> dict[str, float]:
        return {"step_up": self.step_up, "hold": self.hold, "block": self.block}


@dataclass(frozen=True)
class PolicyInput:
    rule_score: float = 0.0
    ml_score: float | None = None
    anomaly_score: float | None = None
    signals: Mapping[str, float] = field(default_factory=dict)
    action_hint: str | None = None
    sanctions_hit: bool = False
    account_blocked: bool = False
    unknown_customer: bool = False
    #: typology cap (e.g. APP / mule → at most HOLD: hold and warn, don't block)
    cap: str | None = None
    #: transaction amount (TRY) — a low-risk, low-amount HOLD floor is softened
    amount_try: float = 0.0


@dataclass(frozen=True)
class PolicyDecision:
    decision: str
    risk_score: float
    stacked: float
    base_decision: str
    overrides: tuple[str, ...] = ()
    case_required: bool = False
    hold_minutes: int | None = None

    @property
    def legacy(self) -> str:
        return LEGACY[self.decision]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "risk_score": self.risk_score,
            "stacked": self.stacked,
            "base_decision": self.base_decision,
            "overrides": list(self.overrides),
            "case_required": self.case_required,
            "hold_minutes": self.hold_minutes,
        }


def default_thresholds() -> Thresholds:
    s = get_settings()
    return Thresholds(s.policy_step_up, s.policy_hold, s.policy_block)


class PolicyEngine:
    def __init__(
        self,
        stacker: Stacker | None = None,
        thresholds: Thresholds | None = None,
        weights: Mapping[str, float] | None = None,
    ) -> None:
        settings = get_settings()
        self.stacker = stacker
        self.thresholds = thresholds or default_thresholds()
        self.weights = dict(weights if weights is not None else settings.policy_signal_weights)

    def set_thresholds(self, step_up: float, hold: float, block: float) -> Thresholds:
        self.thresholds = Thresholds(step_up, hold, block)
        return self.thresholds

    # --- scoring ------------------------------------------------------------------
    def stack(self, rule: float, ml: float | None, anomaly: float | None) -> float:
        if self.stacker is not None and ml is not None:
            return self.stacker.predict(rule, ml, anomaly if anomaly is not None else 0.5)
        # Rules-only fallback (no trained model available).
        return rule

    def combine(self, stacked: float, signals: Mapping[str, float]) -> float:
        weighted = (self.weights.get(k, 0.0) * min(1.0, max(0.0, v)) for k, v in signals.items())
        return noisy_or([stacked, *weighted])

    # --- decisions ------------------------------------------------------------------
    def decide(self, inp: PolicyInput) -> PolicyDecision:
        settings = get_settings()
        if inp.account_blocked:
            return PolicyDecision("BLOCK", 1.0, 1.0, "BLOCK", ("ACCOUNT_BLOCKED",))
        if inp.unknown_customer:
            risk = settings.unknown_customer_risk
            return PolicyDecision(
                "HOLD",
                risk,
                risk,
                self.thresholds.action(risk),
                ("UNKNOWN_CUSTOMER",),
                case_required=True,
                hold_minutes=settings.hold_cooling_off_minutes,
            )
        stacked = self.stack(inp.rule_score, inp.ml_score, inp.anomaly_score)
        risk = round(self.combine(stacked, inp.signals), 6)
        base = self.thresholds.action(risk)
        decision = base
        overrides: list[str] = []
        floor = inp.action_hint
        if (
            floor == "HOLD"
            and risk < settings.rule_floor_hold_min_risk
            and inp.amount_try < settings.rule_floor_hold_min_amount_try
        ):
            # thin-history customers (first transfers of the day, new payees) trip
            # pattern rules easily: challenge them instead of freezing the account
            floor = "STEP_UP"
            overrides.append("RULE_FLOOR_SOFTENED")
        if floor and ACTIONS.index(floor) > ACTIONS.index(decision):
            decision = floor
            overrides.append("RULE_FLOOR")
        if inp.cap and ACTIONS.index(decision) > ACTIONS.index(inp.cap):
            decision = inp.cap
            overrides.append("TYPOLOGY_CAP")
        case_required = decision in ("HOLD", "BLOCK")
        if inp.sanctions_hit:
            overrides.append("SANCTIONS_HIT")
            case_required = True
            decision = strongest_action([decision, "HOLD"]) or "HOLD"
        return PolicyDecision(
            decision,
            risk,
            round(stacked, 6),
            base,
            tuple(overrides),
            case_required=case_required,
            hold_minutes=settings.hold_cooling_off_minutes if decision == "HOLD" else None,
        )

    def decide_from_risk(
        self,
        risk: float,
        *,
        force_review: bool = False,
        account_blocked: bool = False,
    ) -> PolicyDecision:
        """Decision for an event that only carries a final risk (legacy producers)."""
        if account_blocked:
            return PolicyDecision("BLOCK", 1.0, 1.0, "BLOCK", ("ACCOUNT_BLOCKED",))
        base = self.thresholds.action(risk)
        decision = base
        overrides: tuple[str, ...] = ()
        if force_review and ACTIONS.index(decision) < ACTIONS.index("HOLD"):
            decision, overrides = "HOLD", ("FORCED_REVIEW",)
        return PolicyDecision(
            decision,
            risk,
            risk,
            base,
            overrides,
            case_required=decision in ("HOLD", "BLOCK"),
            hold_minutes=get_settings().hold_cooling_off_minutes if decision == "HOLD" else None,
        )
