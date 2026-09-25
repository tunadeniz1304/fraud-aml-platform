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

from typing import Literal

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


def simulated_feedback(decision: str, label: int) -> Feedback | None:
    """Feedback the backfill assumes for a historical event.

    Mirrors what production receives right after scoring: a ``STEP_UP``
    challenge is passed by the genuine customer and failed by a fraudster.
    Held / blocked events wait for an analyst and teach nothing in replay.
    """
    if decision == "STEP_UP":
        return "step_up_passed" if label == 0 else "step_up_failed"
    return None
