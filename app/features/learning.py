"""The single profile-learning rule shared by training backfill and live scoring.

A customer's adaptive profile (known devices, amount / hour / channel habits)
must only learn from events the bank trusts, otherwise a fraudster "teaches"
the profile (poisoning). Before v2 the backfill learned from ``label == 0``
while the live engine learned from ``decision == ALLOW`` — two different
rules, so training features drifted from serving features (train/serve skew)
and a legitimate new device that passed a step-up was never learned (false
positive loop). Both paths now call :func:`should_learn`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Feedback = Literal["step_up_passed", "step_up_failed", "clean", "fraud"]

#: feedback that proves the customer (not a fraudster) made the payment
TRUSTED_FEEDBACK: frozenset[str] = frozenset({"step_up_passed", "clean"})
#: feedback that proves the opposite — never learn, whatever the decision was
DISTRUSTED_FEEDBACK: frozenset[str] = frozenset({"step_up_failed", "fraud"})


def should_learn(decision: str, feedback: Feedback | None = None) -> bool:
    """Whether the profile may learn from an event.

    * confirmed fraud / failed step-up → never;
    * passed step-up (OTP) or an analyst's "clean" label → always, so the new
      device and payee become verified;
    * no feedback yet → only an ``ALLOW`` decision is trusted.
    """
    if feedback in DISTRUSTED_FEEDBACK:
        return False
    if feedback in TRUSTED_FEEDBACK:
        return True
    return decision == "ALLOW"


#: step-up (OTP) pass rate of a fraudulent payment by typology. In an APP scam
#: the *victim* authorises the payment and passes the OTP; mules and smurfs
#: operate their own accounts. Only account takeovers / card testing face a
#: challenge they mostly fail (SIM swap and OTP phishing still get through).
DEFAULT_FRAUD_PASS_RATES: dict[str, float] = {"app": 0.9, "mule": 1.0, "structuring": 1.0}


@dataclass(frozen=True)
class StepUpOutcomeModel:
    """Non-oracle step-up (OTP) outcome for offline replays.

    Before v3 the backfill and the evaluation replays read the step-up result
    off the label (genuine → passed, fraud → failed): a perfect oracle that
    taught every legitimate new device and never a fraudulent one, which
    inflated replay metrics. The outcome is now drawn from a fixed model that
    does not look at any classifier output:

    * a genuine customer fails / abandons the challenge with probability
      ``genuine_fail_rate`` (no SMS, lost phone, gives up);
    * a fraudulent payment passes with ``fraud_pass_rate`` (per-typology
      overrides in ``typology_pass_rates``).

    The draw is a hash of ``(seed, transaction_id)``, so backfill, parity
    replay and evaluation replays see the same outcome for the same event.
    The ground truth is the generator's truth (a noisy label is undone with the
    ``label_noise`` flag): the OTP is passed by whoever is actually paying,
    not by whatever the chargeback label later says.
    """

    fraud_pass_rate: float = 0.30
    genuine_fail_rate: float = 0.05
    typology_pass_rates: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FRAUD_PASS_RATES)
    )
    seed: int = 0

    def _uniform(self, key: str) -> float:
        digest = hashlib.blake2b(f"otp:{self.seed}:{key}".encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") / 2**64

    def feedback(self, decision: str, tx: Mapping[str, Any]) -> Feedback | None:
        """Outcome of the challenge a ``STEP_UP`` decision triggers.

        Held / blocked events wait for an analyst and teach nothing in replay.
        """
        if decision != "STEP_UP":
            return None
        fraud = int(tx.get("label", 0)) ^ int(bool(tx.get("label_noise")))
        u = self._uniform(str(tx.get("transaction_id", "")))
        if fraud:
            typology = str(tx.get("typology") or "")
            passed = u < self.typology_pass_rates.get(typology, self.fraud_pass_rate)
        else:
            passed = u >= self.genuine_fail_rate
        return "step_up_passed" if passed else "step_up_failed"


#: the outcome model every offline replay uses (training backfill, parity
#: check, public-data and synthetic evaluation replays)
DEFAULT_STEP_UP_MODEL = StepUpOutcomeModel()
