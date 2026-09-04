"""Time helpers.

All timestamps are stored and compared in UTC; the tenant timezone is applied
only when rendering for humans or computing working-hour SLAs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from civicos.core.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Coerce a naive datetime (as SQLite returns) to UTC-aware."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def get_zone(name: str | None = None) -> tzinfo:
    """Resolve a timezone, degrading to UTC rather than raising.

    Windows and slim containers ship no system tz database, so ``ZoneInfo``
    can fail even for ``"UTC"`` when the ``tzdata`` package is absent. A
    municipality's reports must not stop because of that, so the final
    fallback is the stdlib UTC object.
    """
    candidate = name or get_settings().timezone
    for key in (candidate, "UTC"):
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return UTC


def to_local(value: datetime, timezone: str | None = None) -> datetime:
    return ensure_utc(value).astimezone(get_zone(timezone))


def local_date(timezone: str | None = None) -> date:
    return to_local(utcnow(), timezone).date()


def isoformat(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat() if value else None


def humanize_delta(delta: timedelta) -> str:
    """Render a duration the way a dashboard would: ``2d 4h``, ``38m``."""
    seconds = int(abs(delta.total_seconds()))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def business_minutes_between(
    start: datetime,
    end: datetime,
    *,
    timezone: str | None = None,
    workday_start: time = time(9, 0),
    workday_end: time = time(17, 0),
    weekend_days: frozenset[int] = frozenset({6}),  # tenant-configurable
) -> int:
    """Minutes of working time between two instants.

    SLA clocks for non-emergency categories only tick during office hours, so a
    complaint filed at 23:50 on Saturday is not already breached by Monday.
    """
    start = to_local(start, timezone)
    end = to_local(end, timezone)
    if end <= start:
        return 0

    total = 0
    cursor = start
    while cursor.date() <= end.date():
        day = cursor.date()
        if day.weekday() not in weekend_days:
            day_start = datetime.combine(day, workday_start, tzinfo=cursor.tzinfo)
            day_end = datetime.combine(day, workday_end, tzinfo=cursor.tzinfo)
            window_start = max(cursor, day_start)
            window_end = min(end, day_end)
            if window_end > window_start:
                total += int((window_end - window_start).total_seconds() // 60)
        cursor = datetime.combine(day + timedelta(days=1), time.min, tzinfo=cursor.tzinfo)
    return total


def add_business_minutes(
    start: datetime,
    minutes: int,
    *,
    timezone: str | None = None,
    workday_start: time = time(9, 0),
    workday_end: time = time(17, 0),
    weekend_days: frozenset[int] = frozenset({6}),
) -> datetime:
    """Project a working-hours deadline forward from ``start``."""
    cursor = to_local(start, timezone)
    remaining = minutes
    guard = 0
    while remaining > 0 and guard < 400:  # ~1 year of workdays
        guard += 1
        day = cursor.date()
        if day.weekday() in weekend_days:
            cursor = datetime.combine(day + timedelta(days=1), workday_start, tzinfo=cursor.tzinfo)
            continue
        day_start = datetime.combine(day, workday_start, tzinfo=cursor.tzinfo)
        day_end = datetime.combine(day, workday_end, tzinfo=cursor.tzinfo)
        if cursor < day_start:
            cursor = day_start
        if cursor >= day_end:
            cursor = datetime.combine(day + timedelta(days=1), workday_start, tzinfo=cursor.tzinfo)
            continue
        available = int((day_end - cursor).total_seconds() // 60)
        if remaining <= available:
            return ensure_utc(cursor + timedelta(minutes=remaining))
        remaining -= available
        cursor = datetime.combine(day + timedelta(days=1), workday_start, tzinfo=cursor.tzinfo)
    return ensure_utc(cursor)


def start_of_day(value: datetime, timezone: str | None = None) -> datetime:
    local = to_local(value, timezone)
    return ensure_utc(datetime.combine(local.date(), time.min, tzinfo=local.tzinfo))


def day_range(days: int, timezone: str | None = None) -> tuple[datetime, datetime]:
    """``(start, end)`` covering the last ``days`` complete-and-current days."""
    end = utcnow()
    start = start_of_day(end - timedelta(days=days - 1), timezone)
    return start, end
