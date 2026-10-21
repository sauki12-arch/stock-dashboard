from datetime import date, datetime, timezone

import pytest

from aiscanner.data import market_calendar as cal


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_2026_holidays():
    h = cal.holidays(2026)
    assert date(2026, 4, 3) in h                    # Good Friday
    assert date(2026, 7, 3) in h and date(2026, 7, 4) not in h   # Jul 4 is Saturday -> Friday observed
    assert date(2026, 11, 26) in h                  # Thanksgiving
    assert date(2026, 1, 19) in h                   # MLK
    assert date(2026, 5, 25) in h                   # Memorial
    assert date(2026, 6, 19) in h                   # Juneteenth (Friday)
    assert date(2026, 12, 25) in h
    assert len(h) == 10


def test_new_year_on_saturday_not_observed_on_friday():
    assert date(2021, 12, 31) not in cal.holidays(2021)   # Jan 1 2022 was a Saturday
    assert date(2022, 1, 1) not in cal.holidays(2022)


def test_sunday_holiday_moves_to_monday():
    assert date(2023, 1, 2) in cal.holidays(2023)         # Jan 1 2023 is a Sunday


def test_easter_known_dates():
    assert cal._easter(2024) == date(2024, 3, 31)
    assert cal._easter(2025) == date(2025, 4, 20)


def test_early_close_days():
    assert cal.is_early_close(date(2025, 11, 28))
    assert cal.is_early_close(date(2025, 12, 24))
    assert not cal.is_early_close(date(2025, 12, 26))
    assert cal.session_close(date(2025, 11, 28)).hour == 13


def test_session_close_utc_across_dst():
    # before DST (EST, UTC-5) and after (EDT, UTC-4)
    assert cal.session_close(date(2026, 3, 6)).astimezone(timezone.utc).hour == 21
    assert cal.session_close(date(2026, 3, 9)).astimezone(timezone.utc).hour == 20
    # autumn transition
    assert cal.session_close(date(2026, 10, 30)).astimezone(timezone.utc).hour == 20
    assert cal.session_close(date(2026, 11, 2)).astimezone(timezone.utc).hour == 21


def test_last_completed_session_cases():
    # Saturday -> Friday
    assert cal.last_completed_session(utc(2026, 3, 7, 15)) == date(2026, 3, 6)
    # Friday 15:00 ET (20:00 UTC, EST) -> still Thursday
    assert cal.last_completed_session(utc(2026, 3, 6, 20, 0)) == date(2026, 3, 5)
    # Friday 16:10 ET with 20-min grace -> not final yet; 16:30 -> final
    assert cal.last_completed_session(utc(2026, 3, 6, 21, 10), 20) == date(2026, 3, 5)
    assert cal.last_completed_session(utc(2026, 3, 6, 21, 30), 20) == date(2026, 3, 6)
    # Monday holiday (MLK 2026-01-19) -> previous Friday
    assert cal.last_completed_session(utc(2026, 1, 19, 18)) == date(2026, 1, 16)
    # early close: 13:30 ET on Black Friday 2025 is already final
    assert cal.last_completed_session(utc(2025, 11, 28, 18, 30)) == date(2025, 11, 28)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        cal.last_completed_session(datetime(2026, 3, 6, 12))


def test_market_state():
    assert cal.market_state(utc(2026, 3, 10, 13, 0)) == "pre"      # 09:00 EDT
    assert cal.market_state(utc(2026, 3, 10, 15, 0)) == "open"
    assert cal.market_state(utc(2026, 3, 10, 21, 0)) == "after"
    assert cal.market_state(utc(2026, 3, 8, 15, 0)) == "closed"


def test_sessions_behind_and_stale():
    now = utc(2026, 3, 10, 21, 30)
    assert cal.sessions_behind(date(2026, 3, 10), now) == 0
    assert cal.sessions_behind(date(2026, 3, 9), now) == 1
    assert cal.sessions_behind(date(2026, 3, 5), now) == 3          # 6, 9, 10
    assert not cal.is_stale(date(2026, 3, 9), now, max_stale_sessions=1)
    assert cal.is_stale(date(2026, 3, 9), now, max_stale_sessions=0)
    assert cal.is_stale(date(2026, 3, 5), now, max_stale_sessions=1)


def test_navigation():
    assert cal.next_trading_day(date(2026, 7, 2)) == date(2026, 7, 6)
    assert cal.prev_trading_day(date(2026, 7, 6)) == date(2026, 7, 2)
    assert cal.week_last_trading_day(date(2026, 7, 1)) == date(2026, 7, 2)
