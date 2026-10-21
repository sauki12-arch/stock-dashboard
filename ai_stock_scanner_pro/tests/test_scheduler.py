from datetime import date, datetime, timezone

import pytest

from aiscanner import scheduler as S
from aiscanner.config import ScheduleCfg
from aiscanner.data import market_calendar as cal

CFG = ScheduleCfg(enabled=True)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_pre_market_instant_follows_dst():
    est = S.scheduled_instant("pre_market", date(2026, 3, 6), CFG)       # EST (UTC-5) -> 08:30 ET = 13:30 UTC
    edt = S.scheduled_instant("pre_market", date(2026, 3, 9), CFG)       # EDT (UTC-4) -> 12:30 UTC
    assert est.astimezone(timezone.utc).hour == 13 and edt.astimezone(timezone.utc).hour == 12
    assert est.hour == edt.hour == 8 and est.minute == 30


def test_post_close_instants_normal_and_early_close():
    assert S.scheduled_instant("post_close", date(2026, 3, 10), CFG).strftime("%H:%M") == "16:30"
    assert S.scheduled_instant("post_close", date(2025, 11, 28), CFG).strftime("%H:%M") == "13:30"    # early close


def test_no_daily_jobs_on_holidays_and_weekends():
    for d in (date(2026, 4, 3), date(2026, 3, 7), date(2026, 3, 8)):
        assert S.scheduled_instant("pre_market", d, CFG) is None and S.scheduled_instant("post_close", d, CFG) is None


def test_weekly_review_saturday_0900_et():
    s = S.scheduled_instant("weekly_review", date(2026, 3, 7), CFG)
    assert s.strftime("%a %H:%M") == "Sat 09:00"
    assert S.scheduled_instant("weekly_review", date(2026, 3, 6), CFG) is None


WEEKLY_DONE = {"weekly_review": datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)}


def test_due_jobs_basic_flow():
    last = dict(WEEKLY_DONE)
    # Tuesday 08:45 ET (12:45 UTC, EDT) -> pre_market is due
    assert S.due_jobs(utc(2026, 3, 10, 12, 45), last, CFG) == ["pre_market"]
    last["pre_market"] = utc(2026, 3, 10, 12, 46)
    assert S.due_jobs(utc(2026, 3, 10, 12, 50), last, CFG) == []
    # 16:40 ET -> post_close
    assert "post_close" in S.due_jobs(utc(2026, 3, 10, 20, 40), last, CFG)
    last["post_close"] = utc(2026, 3, 10, 20, 41)
    assert S.due_jobs(utc(2026, 3, 10, 20, 50), last, CFG) == []


def test_disabled_schedule_has_no_jobs():
    assert S.due_jobs(utc(2026, 3, 10, 20, 40), {}, ScheduleCfg(enabled=False)) == []


def test_pre_market_skipped_once_market_open_but_catchup_for_post_close():
    # 10:00 ET Tuesday: pre-market window gone; yesterday's post_close (>12h ago) is beyond catch-up too
    assert S.due_jobs(utc(2026, 3, 10, 14, 0), dict(WEEKLY_DONE), CFG) == []
    # app started 19:00 ET -> post_close missed 2.5h ago -> catch up
    assert S.due_jobs(utc(2026, 3, 10, 23, 0), dict(WEEKLY_DONE), CFG) == ["post_close"]


def test_weekly_review_due_and_not_repeated():
    sat = utc(2026, 3, 7, 15, 0)                       # 10:00 ET
    assert "weekly_review" in S.due_jobs(sat, {}, CFG)
    assert "weekly_review" not in S.due_jobs(sat, {"weekly_review": utc(2026, 3, 7, 14, 30)}, CFG)
    # still due on Monday if it never ran
    assert "weekly_review" in S.due_jobs(utc(2026, 3, 9, 15, 0), {}, CFG)


def test_intraday_only_when_open_and_interval_elapsed():
    cfg = ScheduleCfg(enabled=True, intraday_enabled=True, intraday_minutes=30)
    openn = utc(2026, 3, 10, 15, 0)                   # 11:00 ET
    assert "intraday" in S.due_jobs(openn, {}, cfg)
    assert "intraday" not in S.due_jobs(openn, {"intraday": utc(2026, 3, 10, 14, 45)}, cfg)
    assert "intraday" in S.due_jobs(openn, {"intraday": utc(2026, 3, 10, 14, 20)}, cfg)
    assert "intraday" not in S.due_jobs(utc(2026, 3, 10, 22, 0), {}, cfg)       # closed


def test_next_runs_skips_weekend_and_dst_gap():
    nr = S.next_runs(utc(2026, 3, 6, 22, 0), CFG)                              # Friday evening ET
    assert nr["pre_market"].date() == date(2026, 3, 9)                         # Monday (DST started Sunday)
    assert nr["pre_market"].astimezone(timezone.utc).hour == 12
    assert nr["weekly_review"].date() == date(2026, 3, 7)


def test_dst_fall_back_week():
    # 2026-11-01 DST ends; Monday 11-02 pre-market 08:30 EST = 13:30 UTC
    nr = S.next_runs(utc(2026, 10, 31, 20, 0), CFG)
    assert nr["pre_market"].date() == date(2026, 11, 2) and nr["pre_market"].astimezone(timezone.utc).hour == 13
