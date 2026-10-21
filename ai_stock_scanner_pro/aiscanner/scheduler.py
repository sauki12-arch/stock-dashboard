"""Schedule logic in America/New_York (DST-safe via zoneinfo). Pure functions: the caller supplies `now` and last runs.

Jobs
  pre_market    : `pre_market_minutes_before_open` before the open of each trading day (default 08:30 ET)
  post_close    : `post_close_minutes_after_close` after the close (16:30 ET; 13:30 ET on early-close days)
  intraday      : every `intraday_minutes` while the market is open (optional; monitoring only)
  weekly_review : weekly at `weekly_review_time` ET on `weekly_review_weekday` (default Saturday 09:00)

Missed jobs are caught up (post_close/pre_market within a window; weekly until the next scheduled instant) because the
app is a desktop program that may be closed at the scheduled time. No Windows service exists – see README.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .config import ScheduleCfg, parse_hhmm
from .data import market_calendar as cal

JOBS = ("pre_market", "post_close", "intraday", "weekly_review")
CATCHUP_HOURS = 12


def _at(d: date, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime.combine(d, time(h, m), tzinfo=cal.NY)


def scheduled_instant(job: str, d: date, cfg: ScheduleCfg) -> datetime | None:
    """The scheduled wall-clock instant of `job` for NY date d (None if it does not apply that day)."""
    if job == "pre_market":
        return cal.session_open(d) - timedelta(minutes=cfg.pre_market_minutes_before_open) if cal.is_trading_day(d) else None
    if job == "post_close":
        return cal.session_close(d) + timedelta(minutes=cfg.post_close_minutes_after_close) if cal.is_trading_day(d) else None
    if job == "weekly_review":
        return _at(d, cfg.weekly_review_time) if d.weekday() == cfg.weekly_review_weekday else None
    raise ValueError(job)


def latest_scheduled(job: str, now: datetime, cfg: ScheduleCfg) -> datetime | None:
    """Most recent scheduled instant ≤ now (searching back up to 10 days)."""
    n = cal.to_ny(now)
    for back in range(0, 11):
        s = scheduled_instant(job, n.date() - timedelta(days=back), cfg)
        if s is not None and s <= n:
            return s
    return None


def due_jobs(now: datetime, last_runs: dict[str, datetime | None], cfg: ScheduleCfg) -> list[str]:
    if not cfg.enabled:
        return []
    n = cal.to_ny(now)
    due: list[str] = []

    def last(job: str) -> datetime | None:
        v = last_runs.get(job)
        return cal.to_ny(v) if v else None

    for job in ("pre_market", "post_close"):
        s = latest_scheduled(job, n, cfg)
        if s is None or n - s > timedelta(hours=CATCHUP_HOURS):
            continue
        if job == "pre_market" and n >= cal.session_open(n.date()) and s.date() == n.date():
            continue                                   # market already open – the pre-market scan is moot
        lr = last(job)
        if lr is None or lr < s:
            due.append(job)
    s = latest_scheduled("weekly_review", n, cfg)
    if s is not None and (last("weekly_review") is None or last("weekly_review") < s):
        due.append("weekly_review")
    if cfg.intraday_enabled and cal.market_state(n) == "open":
        lr = last("intraday")
        if lr is None or n - lr >= timedelta(minutes=cfg.intraday_minutes):
            due.append("intraday")
    return due


def next_runs(now: datetime, cfg: ScheduleCfg, horizon_days: int = 10) -> dict[str, datetime | None]:
    n = cal.to_ny(now)
    out: dict[str, datetime | None] = {}
    for job in ("pre_market", "post_close", "weekly_review"):
        out[job] = None
        for fwd in range(0, horizon_days + 1):
            s = scheduled_instant(job, n.date() + timedelta(days=fwd), cfg)
            if s is not None and s > n:
                out[job] = s
                break
    if cfg.intraday_enabled:
        if cal.market_state(n) == "open":
            out["intraday"] = n + timedelta(minutes=cfg.intraday_minutes)
        else:
            d = n.date() if cal.market_state(n) == "pre" else cal.next_trading_day(n.date())
            out["intraday"] = cal.session_open(d)
    return out
