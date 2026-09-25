"""SLA clocks: MASAK business-day deadline and the internal review SLA.

MASAK (5549 sayılı Kanun md. 4; Tedbirler Yönetmeliği md. 28/2): a suspicious
transaction must be reported "en geç on iş günü içinde" from the moment the
suspicion arises. Weekends and Turkish public holidays are skipped. Holidays
come from the ``holidays`` package (``holidays.Turkey``) for any year, including
the moving religious holidays; the arife half days (``saat 13.00'ten``) follow
``tr_half_day_policy``: ``business_day`` (default — the morning is worked, so the
deadline is never later than the law allows) or ``holiday``. ``tr_holidays``
adds ad-hoc closures (e.g. administrative leave). The internal SLA (first
analyst action) defaults to 4 hours.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from functools import lru_cache

from app.config import get_settings


@lru_cache(maxsize=64)
def _official(year: int, half_days_off: bool) -> frozenset[date]:
    import holidays as holiday_lib

    categories = ("public", "half_day") if half_days_off else ("public",)
    return frozenset(holiday_lib.Turkey(years=year, categories=categories))


def holidays(year: int | None = None) -> frozenset[date]:
    """Non-business days of ``year`` (default: this year) + configured extras."""
    settings = get_settings()
    year = year or date.today().year
    extra = {d for d in (date.fromisoformat(x) for x in settings.tr_holidays) if d.year == year}
    return _official(year, settings.tr_half_day_policy == "holiday") | extra


def _off(day: date, extra: Iterable[date] | None) -> bool:
    if extra is not None:
        return day in frozenset(extra)
    return day in holidays(day.year)


def is_business_day(day: date, extra: Iterable[date] | None = None) -> bool:
    return day.weekday() < 5 and not _off(day, extra)


def add_business_days(start: datetime, days: int, extra: Iterable[date] | None = None) -> datetime:
    """``start`` + ``days`` business days (same time of day)."""
    fixed = frozenset(extra) if extra is not None else None
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if is_business_day(current.date(), fixed):
            remaining -= 1
    return current


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole business days from ``start`` (exclusive) to ``end`` (inclusive)."""
    if end <= start:
        return 0
    count = 0
    day = start.date()
    while day < end.date():
        day += timedelta(days=1)
        if is_business_day(day):
            count += 1
    return count


def masak_deadline(suspicion_at: datetime) -> datetime:
    return add_business_days(suspicion_at, get_settings().masak_business_days)


def internal_sla_due(opened_at: datetime) -> datetime:
    return opened_at + timedelta(hours=get_settings().internal_sla_hours)
