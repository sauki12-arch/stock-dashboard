"""NYSE/Nasdaq trading calendar computed from rules (no external calendar dependency).

Covered: weekends, the 10 standard full-day holidays with weekend "observed" rules,
and the standard 13:00 early closes (day after Thanksgiving, Dec 24, Jul 3).
NOT covered: ad-hoc closures (e.g. national days of mourning, hurricanes). Treat the
calendar as best-effort; stale-data detection is the second line of defence.
All wall-clock logic uses America/New_York so DST transitions are handled by zoneinfo.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OPEN_T = time(9, 30)
CLOSE_T = time(16, 0)
EARLY_CLOSE_T = time(13, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:  # Anonymous Gregorian algorithm
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date, saturday_to_friday: bool = True) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    h: set[date | None] = set()
    # New Year's Day: when it falls on Saturday NYSE does not close on the Friday before.
    h.add(_observed(date(year, 1, 1), saturday_to_friday=False))
    h.add(_nth_weekday(year, 1, 0, 3))                 # MLK
    h.add(_nth_weekday(year, 2, 0, 3))                 # Presidents' Day
    h.add(_easter(year) - timedelta(days=2))           # Good Friday
    h.add(_last_weekday(year, 5, 0))                   # Memorial Day
    if year >= 2022:
        h.add(_observed(date(year, 6, 19)))            # Juneteenth
    h.add(_observed(date(year, 7, 4)))                 # Independence Day
    h.add(_nth_weekday(year, 9, 0, 1))                 # Labor Day
    h.add(_nth_weekday(year, 11, 3, 4))                # Thanksgiving
    h.add(_observed(date(year, 12, 25)))               # Christmas
    return frozenset(x for x in h if x is not None)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def is_early_close(d: date) -> bool:
    if not is_trading_day(d):
        return False
    if d.month == 11 and d.weekday() == 4 and d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return True
    if d.month == 12 and d.day == 24:
        return True
    return d.month == 7 and d.day == 3


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def session_open(d: date) -> datetime:
    return datetime.combine(d, OPEN_T, tzinfo=NY)


def session_close(d: date) -> datetime:
    return datetime.combine(d, EARLY_CLOSE_T if is_early_close(d) else CLOSE_T, tzinfo=NY)


def to_ny(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("naive datetime not allowed; pass tz-aware time")
    return now.astimezone(NY)


def now_utc() -> datetime:
    return datetime.now(UTC)


def last_completed_session(now: datetime, grace_minutes: int = 0) -> date:
    """Most recent trading date whose bar is final (close + grace has passed)."""
    n = to_ny(now)
    d = n.date()
    if is_trading_day(d) and n >= session_close(d) + timedelta(minutes=grace_minutes):
        return d
    return prev_trading_day(d)


def market_state(now: datetime) -> str:
    """'open' | 'pre' | 'after' | 'closed' (non-trading day)."""
    n = to_ny(now)
    d = n.date()
    if not is_trading_day(d):
        return "closed"
    if n < session_open(d):
        return "pre"
    if n < session_close(d):
        return "open"
    return "after"


def sessions_behind(last_bar: date, now: datetime, grace_minutes: int = 0) -> int:
    """How many completed sessions are missing after `last_bar` (0 = up to date)."""
    target = last_completed_session(now, grace_minutes)
    if last_bar >= target:
        return 0
    return len(trading_days(last_bar + timedelta(days=1), target))


def is_stale(last_bar: date, now: datetime, max_stale_sessions: int = 1, grace_minutes: int = 0) -> bool:
    """Stale = more than `max_stale_sessions` completed sessions missing (0 = must be current)."""
    return sessions_behind(last_bar, now, grace_minutes) > max_stale_sessions


def week_last_trading_day(d: date) -> date:
    """Last trading day in the calendar week containing d."""
    friday = d + timedelta(days=4 - d.weekday())
    while not is_trading_day(friday):
        friday -= timedelta(days=1)
    return friday
