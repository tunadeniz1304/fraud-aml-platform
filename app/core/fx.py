"""Currency normalisation to TRY (fixes bug #12: TRY vs EUR comparisons).

Rates come from ``Settings.fx_rates_try`` (1 unit = X TRY); money is handled as
``Decimal`` and rounded half-up to kuruş.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.config import get_settings

KURUS = Decimal("0.01")


class UnknownCurrencyError(ValueError):
    """Currency code has no configured TRY rate."""


def to_decimal(value: float | int | str | Decimal) -> Decimal:
    try:
        return Decimal(str(value)).quantize(KURUS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Geçersiz tutar: {value!r}") from exc


def rate_to_try(currency: str, rates: dict[str, float] | None = None) -> Decimal:
    table = rates if rates is not None else get_settings().fx_rates_try
    code = (currency or "TRY").strip().upper()
    if code not in table:
        raise UnknownCurrencyError(f"'{code}' için kur tanımlı değil")
    return Decimal(str(table[code]))


def to_try(
    amount: float | int | str | Decimal, currency: str, rates: dict[str, float] | None = None
) -> Decimal:
    """Convert ``amount`` in ``currency`` to TRY (Decimal, kuruş precision)."""
    return (to_decimal(amount) * rate_to_try(currency, rates)).quantize(
        KURUS, rounding=ROUND_HALF_UP
    )


def is_supported(currency: str) -> bool:
    return (currency or "").strip().upper() in get_settings().fx_rates_try
