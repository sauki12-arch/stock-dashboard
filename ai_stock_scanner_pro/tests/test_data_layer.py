from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from aiscanner.config import DataCfg
from aiscanner.data import market_calendar as cal
from aiscanner.data.engine import DataEngine, FAILED, REFRESHED, UP_TO_DATE, UPDATED
from aiscanner.data.mock import MockProvider
from aiscanner.data.provider import ProviderError, ProviderUnavailable
from aiscanner.data.ratelimit import RateLimiter, retry_with_backoff
from aiscanner.data.store import Store
from aiscanner.data.universe import load_universe
from aiscanner.data.validation import (corwin_schultz_spread, drop_incomplete_bars, validate_daily)
from aiscanner.data.yahoo import YahooProvider


def make_engine(clock, **kw):
    p = MockProvider(now=clock)
    for k, v in kw.items():
        setattr(p, k, v)
    st = Store(":memory:")
    return p, st, DataEngine(p, st, DataCfg(max_workers=2, batch_size=5), clock=clock, sleep=lambda s: None)


# ---------------- rate limit / retry --------------------------------
def test_rate_limiter_spaces_calls():
    t = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
    rl = RateLimiter(2.0, clock=lambda: t[0], sleep=sleep)
    waits = [rl.acquire() for _ in range(4)]
    assert waits[0] == 0 and waits[1] == pytest.approx(0.5) and waits[2] == pytest.approx(0.5)
    assert abs(t[0] - 1.5) < 1e-9


def test_retry_backoff_sequence_and_exhaustion():
    delays, calls = [], []

    def boom():
        calls.append(1)
        raise ProviderUnavailable("x")
    with pytest.raises(ProviderUnavailable):
        retry_with_backoff(boom, retries=3, base=1.0, retry_on=(ProviderUnavailable,),
                           sleep=delays.append, jitter=lambda: 0.0)
    assert len(calls) == 4 and delays == [1.0, 2.0, 4.0]


def test_retry_succeeds_midway():
    state = {"n": 0}

    def f():
        state["n"] += 1
        if state["n"] < 3:
            raise ProviderUnavailable("x")
        return "ok"
    assert retry_with_backoff(f, retries=5, base=0.1, retry_on=(ProviderUnavailable,), sleep=lambda s: None) == "ok"


def test_non_retryable_exception_not_swallowed():
    with pytest.raises(KeyError):
        retry_with_backoff(lambda: {}["a"], retries=3, base=0.1, retry_on=(ProviderUnavailable,), sleep=lambda s: None)


# ---------------- mock provider -------------------------------------
def test_mock_deterministic_and_no_future(clock):
    p1, p2 = MockProvider(now=clock), MockProvider(now=clock)
    a = p1.fetch_daily(["AAPL"], date(2025, 1, 1), date(2030, 1, 1))["AAPL"]
    b = p2.fetch_daily(["AAPL"], date(2025, 1, 1), date(2030, 1, 1))["AAPL"]
    pd.testing.assert_frame_equal(a, b)
    assert a.index[-1].date() == date(2026, 3, 10)          # last completed session, never later


def test_mock_prefix_stable_when_end_moves():
    early = MockProvider(now=lambda: datetime(2026, 3, 6, 22, tzinfo=timezone.utc))
    late = MockProvider(now=lambda: datetime(2026, 3, 10, 22, tzinfo=timezone.utc))
    a = early.fetch_daily(["MSFT"], date(2025, 6, 1), date(2026, 3, 6))["MSFT"]
    b = late.fetch_daily(["MSFT"], date(2025, 6, 1), date(2026, 3, 6))["MSFT"]
    pd.testing.assert_frame_equal(a, b)


def test_mock_intraday_only_completed_bars():
    now = datetime(2026, 3, 10, 15, 3, tzinfo=timezone.utc)   # 11:03 EDT, market open
    p = MockProvider(now=lambda: now)
    df = p.fetch_intraday("AAPL", "5m", 2)
    last_bar_end = df.index[-1] + pd.Timedelta(minutes=5)
    assert last_bar_end <= pd.Timestamp(now.replace(tzinfo=None))
    assert len(df) > 0


# ---------------- validation ----------------------------------------
def good_frame(n=30, end=date(2026, 3, 10)):
    days = cal.trading_days(end - timedelta(days=60), end)[-n:]
    idx = pd.DatetimeIndex(days)
    c = np.linspace(100, 110, len(idx))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)


def test_validation_clean(now):
    q = validate_daily("X", good_frame(), now)
    assert q.ok and q.issues == [] and q.sessions_behind == 0


def test_validation_detects_defects(now):
    df = good_frame()
    df.iloc[5, df.columns.get_loc("close")] = np.nan
    q = validate_daily("X", df, now)
    assert not q.ok and "nan_prices" in q.codes()
    df = good_frame()
    df.iloc[3, df.columns.get_loc("low")] = -1
    assert "non_positive_price" in validate_daily("X", df, now).codes()
    df = good_frame()
    df = df.drop(df.index[10:13])
    q = validate_daily("X", df, now)
    assert "missing_sessions" in q.codes() and q.ok               # short gap = warning only
    df = good_frame().drop(good_frame().index[5:15])
    assert not validate_daily("X", df, now).ok                    # long gap = critical
    assert "no_data" in validate_daily("X", good_frame().iloc[0:0], now).codes()


def test_validation_stale_and_short(now):
    df = good_frame(end=date(2026, 3, 5))
    q = validate_daily("X", df, now, max_stale_sessions=1)
    assert q.stale and not q.ok and q.sessions_behind == 3
    q = validate_daily("X", good_frame(10), now, min_bars=50)
    assert "insufficient_history" in q.codes()


def test_drop_incomplete_bar_during_session():
    during = datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc)      # 14:00 EDT
    df = good_frame(5)
    out = drop_incomplete_bars(df, during)
    assert out.index[-1].date() == date(2026, 3, 9)                 # today's bar removed
    after = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)
    assert drop_incomplete_bars(df, after).index[-1].date() == date(2026, 3, 10)


def test_corwin_schultz():
    df = good_frame(60)
    s = corwin_schultz_spread(df)
    assert s is not None and 0 <= s < 0.05
    assert corwin_schultz_spread(good_frame(10)) is None
    bad = good_frame(60)
    bad.iloc[-1, bad.columns.get_loc("low")] = 0
    assert corwin_schultz_spread(bad) is None


# ---------------- store ---------------------------------------------
def test_store_roundtrip_and_upsert():
    st = Store(":memory:")
    df = good_frame(10)
    st.upsert_bars("AAA", df)
    out = st.load_bars("AAA")
    pd.testing.assert_frame_equal(out, df, check_freq=False, check_exact=False, check_index_type=False, check_names=False)
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("close")] = 999.0
    st.upsert_bars("AAA", df2)
    assert len(st.load_bars("AAA")) == 10 and st.load_bars("AAA").close.iloc[-1] == 999.0


def test_store_file_db_persists(tmp_path):
    p = tmp_path / "x.db"
    Store(p).upsert_bars("A", good_frame(5))
    assert len(Store(p).load_bars("A")) == 5


# ---------------- engine --------------------------------------------
def test_engine_full_then_incremental(clock):
    p, st, eng = make_engine(clock)
    syms = ["AAPL", "MSFT", "SPY", "XLK", "NVDA", "AMD", "QQQ"]
    r1 = eng.update_daily(syms)
    assert r1.count(UPDATED) == 7 and r1.n_failed == 0
    r2 = eng.update_daily(syms)
    assert r2.count(UP_TO_DATE) == 7 and len(p.calls) == len(p.calls)   # nothing new to fetch
    n_calls = len(p.calls)
    # advance the clock 2 sessions: only a short overlap window is requested
    later = datetime(2026, 3, 12, 21, 30, tzinfo=timezone.utc)
    eng.clock = p._now = lambda: later
    r3 = eng.update_daily(syms)
    assert r3.count(UPDATED) == 7
    new_calls = p.calls[n_calls:]
    assert all((e - s).days <= 10 + 4 for _, s, e in new_calls)
    assert st.last_bar_date("AAPL") == "2026-03-12"


def test_engine_isolates_failures(clock):
    p, st, eng = make_engine(clock, fail_symbols={"BAD1"}, missing_symbols={"GONE"})
    r = eng.update_daily(["AAPL", "BAD1", "GONE", "MSFT"])
    assert r.status["AAPL"] == UPDATED and r.status["MSFT"] == UPDATED
    assert r.status["BAD1"] == FAILED and r.status["GONE"] in (FAILED, "empty")
    assert st.recent_fetch_errors()


def test_engine_retries_flaky_provider(clock):
    p, st, eng = make_engine(clock, flaky_batches=2)
    r = eng.update_daily(["AAPL", "MSFT"])
    assert r.status["AAPL"] == UPDATED and not r.provider_down


def test_engine_provider_down_reported(clock):
    p, st, eng = make_engine(clock, flaky_batches=999)
    r = eng.update_daily(["AAPL", "MSFT", "NVDA"])
    assert r.provider_down and all(v == FAILED for v in r.status.values())


def test_engine_detects_adjustment_revision(clock):
    p, st, eng = make_engine(clock)
    eng.update_daily(["AAPL"])
    d0 = st.load_bars("AAPL").index[-300]
    before = st.load_bars("AAPL").close.loc[d0]
    later = datetime(2026, 3, 12, 21, 30, tzinfo=timezone.utc)
    eng.clock = p._now = lambda: later
    p.scale_before["AAPL"] = (date(2026, 3, 11), 0.5)     # 2:1 split ex-date after our last stored bar
    r = eng.update_daily(["AAPL"])
    assert r.status["AAPL"] == REFRESHED
    after = st.load_bars("AAPL").close.loc[d0]
    assert after == pytest.approx(before * 0.5, rel=1e-6)


def test_engine_drops_partial_bar_during_session():
    now = datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc)      # market open
    p = MockProvider(now=lambda: now)
    st = Store(":memory:")
    eng = DataEngine(p, st, DataCfg(), clock=lambda: now, sleep=lambda s: None)
    eng.update_daily(["AAPL"])
    assert st.last_bar_date("AAPL") == "2026-03-09"


def test_engine_cancel(clock):
    p, st, eng = make_engine(clock)
    r = eng.update_daily(["A", "B", "C", "D", "E", "F", "G", "H"], cancel=lambda: True)
    assert r.cancelled and st.last_bar_date("A") is None


def test_engine_progress_monotonic(clock):
    p, st, eng = make_engine(clock)
    seen = []
    eng.update_daily(["A1", "A2", "A3", "A4", "A5", "A6", "A7"], progress=lambda d, t, m: seen.append(d))
    assert seen == sorted(seen) and seen[-1] == 7


def test_engine_stale_symbol_flagged_in_quality(clock):
    p, st, eng = make_engine(clock, stale_symbols={"OLD": 4})
    eng.update_daily(["OLD", "NEW"])
    assert eng.load("OLD")[1].stale and not eng.load("NEW")[1].stale


def test_engine_intraday_completed_only():
    now = datetime(2026, 3, 10, 15, 3, tzinfo=timezone.utc)
    p = MockProvider(now=lambda: now)
    st = Store(":memory:")
    eng = DataEngine(p, st, DataCfg(), clock=lambda: now, sleep=lambda s: None)
    df = eng.update_intraday("AAPL", "5m", 2)
    assert len(df) > 0 and len(st.load_bars("AAPL", "5m")) == len(df)


def test_engine_intraday_unsupported_provider(clock):
    class NoIntraday(MockProvider):
        supports_intraday = False
    st = Store(":memory:")
    eng = DataEngine(NoIntraday(now=clock), st, clock=clock, sleep=lambda s: None)
    assert isinstance(eng.update_intraday("AAPL"), ProviderError)


# ---------------- yahoo adapter (fake downloader) --------------------
def _yf_frame(symbols, tz=True):
    idx = pd.date_range("2026-03-02", periods=5, freq="B", tz="America/New_York" if tz else None)
    cols = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]])
    data = np.tile(np.array([10, 11, 9, 10.5, 1000.0]), (5, len(symbols)))
    return pd.DataFrame(data, index=idx, columns=cols)


def test_yahoo_multi_symbol_and_missing():
    raw = _yf_frame(["AAA", "BBB"])
    raw[("BBB", "Close")] = np.nan
    raw[("BBB", "Open")] = np.nan
    prov = YahooProvider(downloader=lambda **kw: raw)
    out = prov.fetch_daily(["AAA", "BBB", "ZZZ"], date(2026, 3, 1), date(2026, 3, 8))
    assert len(out["AAA"]) == 5 and out["AAA"].index.tz is None
    assert isinstance(out["BBB"], ProviderError) and isinstance(out["ZZZ"], ProviderError)


def test_yahoo_single_symbol_flat_columns():
    raw = _yf_frame(["AAA"], tz=False)
    raw.columns = raw.columns.get_level_values(1)
    prov = YahooProvider(downloader=lambda **kw: raw)
    assert len(prov.fetch_daily(["AAA"], date(2026, 3, 1), date(2026, 3, 8))["AAA"]) == 5


def test_yahoo_level_swapped_columns():
    raw = _yf_frame(["AAA", "BBB"])
    raw.columns = raw.columns.swaplevel(0, 1)
    prov = YahooProvider(downloader=lambda **kw: raw)
    assert len(prov.fetch_daily(["AAA", "BBB"], date(2026, 3, 1), date(2026, 3, 8))["BBB"]) == 5


def test_yahoo_end_date_is_inclusive_via_plus_one():
    seen = {}
    prov = YahooProvider(downloader=lambda **kw: seen.update(kw) or _yf_frame(["AAA"]))
    prov.fetch_daily(["AAA"], date(2026, 3, 1), date(2026, 3, 8))
    assert seen["end"] == "2026-03-09"


def test_yahoo_intraday_limits():
    seen = {}
    idx = pd.date_range("2026-03-10 09:30", periods=3, freq="5min", tz="America/New_York")
    df = pd.DataFrame({"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0, "Volume": 10}, index=idx)
    prov = YahooProvider(downloader=lambda **kw: seen.update(kw) or df)
    res = prov.fetch_intraday("AAA", "1m", 30)
    assert seen["period"] == "7d" and res.index[0] == pd.Timestamp("2026-03-10 13:30")   # UTC naive
    assert isinstance(prov.fetch_intraday("AAA", "3m", 1), ProviderError)


# ---------------- universe -------------------------------------------
def test_default_universe_is_500_unique():
    u = load_universe()
    assert len(u) == 500 and len(set(u.symbols)) == 500 and not u.errors
    assert set(u.sector.values()) <= set(__import__("aiscanner.data.universe", fromlist=["x"]).SECTOR_ETFS)


def test_universe_csv_validation(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("symbol,sector_etf\naapl,XLK\nAAPL,XLK\nBAD SYM,XLK\nmsft,NOPE\n,\n")
    u = load_universe(p)
    assert u.symbols == ["AAPL", "MSFT"] and len(u.errors) == 3
    (tmp_path / "v.csv").write_text("ticker\nA\n")
    with pytest.raises(ValueError):
        load_universe(tmp_path / "v.csv")
