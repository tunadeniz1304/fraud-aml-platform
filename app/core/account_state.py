"""Account status state machine (fixes bug #4).

``AKTIF → INCELENIYOR → BLOKE`` — automatic (system) transitions are
**monotonic**: they may only escalate. A lower-risk event can never downgrade a
``BLOKE`` account to ``INCELENIYOR``. De-escalation is reserved for a human
analyst decision (and, from F4 on, a maker-checker approval).
"""

from __future__ import annotations

from typing import Literal

AccountStatus = Literal["AKTIF", "INCELENIYOR", "BLOKE"]
STATUSES: tuple[AccountStatus, ...] = ("AKTIF", "INCELENIYOR", "BLOKE")
_RANK: dict[str, int] = {status: i for i, status in enumerate(STATUSES)}

Actor = Literal["system", "analyst"]


class InvalidTransitionError(ValueError):
    """Transition not allowed for this actor."""


def normalise(status: str) -> AccountStatus:
    value = (status or "").strip().upper()
    if value not in _RANK:
        raise ValueError(f"Geçersiz hesap_durumu: {status!r}")
    return value  # type: ignore[return-value]


def rank(status: str) -> int:
    return _RANK[normalise(status)]


def next_status(current: str, requested: str, *, actor: Actor = "system") -> AccountStatus:
    """Return the status the account must move to.

    * ``system``: ``max(current, requested)`` — escalation only; a request to a
      lower status is a no-op (returns ``current``).
    * ``analyst``: any target is allowed (the caller enforces maker-checker).
    """
    cur, req = normalise(current), normalise(requested)
    if actor == "analyst":
        return req
    return req if _RANK[req] > _RANK[cur] else cur


def assert_transition(current: str, target: str, *, actor: Actor) -> None:
    """Raise when ``actor`` may not move ``current`` → ``target``."""
    cur, tgt = normalise(current), normalise(target)
    if actor == "system" and _RANK[tgt] < _RANK[cur]:
        raise InvalidTransitionError(
            f"Sistem {cur} → {tgt} geçişi yapamaz; geri dönüş yalnızca analist kararıyla"
        )
