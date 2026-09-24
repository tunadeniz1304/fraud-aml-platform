"""v1 two-threshold decision mapping (replaced by the policy engine in F3)."""

from __future__ import annotations

from typing import Literal

from app.config import get_settings

LegacyDecision = Literal["BLOKE", "INCELENIYOR", "GECTI"]


def legacy_decision(
    risk: float, *, force_review: bool = False, account_blocked: bool = False
) -> LegacyDecision:
    settings = get_settings()
    if account_blocked or risk >= settings.risk_threshold:
        return "BLOKE"
    if force_review or risk >= settings.warning_threshold:
        return "INCELENIYOR"
    return "GECTI"
