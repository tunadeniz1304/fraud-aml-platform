"""SLA clocks: MASAK business-day deadline and the internal review SLA.

MASAK (5549 sayılı Kanun, ŞİB rehberi): a suspicious transaction must be
reported within **10 business days** of the moment the suspicion arises.
Weekends and the configured public holidays (``TR_HOLIDAYS``) are skipped.
The internal SLA (first analyst action) defaults to 4 hours.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

from app.config import get_settings


def holidays() -> frozenset[date]:
    return frozenset(date.fromisoformat(d) for d in get_settings().tr_holidays)


def is_business_day(day: date, extra: Iterable[date] | None = None) -> bool:
    off = holidays() if extra is None else frozenset(extra)
    return day.weekday() < 5 and day not in off


def add_business_days(start: datetime, days: int, extra: Iterable[date] | None = None) -> datetime:
    """``start`` + ``days`` business days (same time of day)."""
    off = holidays() if extra is None else frozenset(extra)
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5 and current.date() not in off:
            remaining -= 1
    return current


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole business days from ``start`` (exclusive) to ``end`` (inclusive)."""
    if end <= start:
        return 0
    off = holidays()
    count = 0
    day = start.date()
    while day < end.date():
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in off:
            count += 1
    return count


def masak_deadline(suspicion_at: datetime) -> datetime:
    return add_business_days(suspicion_at, get_settings().masak_business_days)


def internal_sla_due(opened_at: datetime) -> datetime:
    return opened_at + timedelta(hours=get_settings().internal_sla_hours)
