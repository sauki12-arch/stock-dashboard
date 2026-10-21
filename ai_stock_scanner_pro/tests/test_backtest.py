import numpy as np
import pandas as pd
import pytest

from aiscanner.backtest import metrics as M
from aiscanner.backtest.engine import BTConfig, build_signals, simulate
from aiscanner.backtest.walkforward import walk_forward
from aiscanner.config import ScanCfg
from tests.helpers import mk, trend

N = 60


def flat_frame(n=N, price=100.0):
    return mk(np.full(n, price), hi=np.full(n, price + 1), lo=np.full(n, price - 1), open_=np.full(n, price))


def set_bar(df, i, o=None, h=None, l=None, c=None):
    for col, v in (("open", o), ("high", h), ("low", l), ("close", c)):
        if v is not None:
            df.iloc[i, df.columns.get_loc(col)] = v
    return df


def sig(df, i, stop=95.0, target=110.0, score=80.0, sym="AAA"):
    return pd.DataFrame([{"symbol": sym, "date": df.index[i], "score": score, "stop": stop, "target": target, "rr": 2.0,
                          "atr": 1.0, "close": float(df["close"].iloc[i])}])


CFG = BTConfig(strategy="swing", max_hold_bars=5, slippage_bps=10, commission=1.0, max_position_pct=1.0,
               max_total_risk_pct=0.5, max_sector_pct=1.0, resize_to_limits=False)


def run(df, signals, cfg=CFG, spy=None, sector_map=None):
    spy = spy if spy is not None else flat_frame(len(df))
    return simulate(signals, {"AAA": df}, spy, cfg, sector_map or {})


def test_entry_is_next_bar_open_with_slippage_and_costs():
    df = flat_frame()
    set_bar(df, 11, o=102.0, h=103.0, l=101.0, c=102.5)              # gap up on the day after the signal
    r = run(df, sig(df, 10, stop=97.0, target=115.0))
    t = r.trades.iloc[0]
    assert t.entry_date == df.index[11]                               # NOT the signal bar
    assert t.entry_px == pytest.approx(102.0 * 1.001)
    exp_shares = int(100_000 * 0.01 // (t.entry_px - 97.0))
    assert t.shares == exp_shares
    assert t.pnl == pytest.approx((t.exit_px - t.entry_px) * t.shares - 2.0)


def test_signal_on_last_bar_never_trades():
    df = flat_frame()
    assert run(df, sig(df, N - 1)).trades.empty


def test_risk_is_capped_at_one_percent_of_equity():
    df = flat_frame()
    r = run(df, sig(df, 10, stop=95.0, target=120.0))
    t = r.trades.iloc[0]
    assert t.shares * (t.entry_px - 95.0) <= 1000.0 + 1e-6


def test_stop_hit_fills_at_stop_minus_slippage():
    df = flat_frame()
    set_bar(df, 13, o=99.0, h=100.0, l=94.0, c=96.0)                  # trades through stop 95 during the day
    t = run(df, sig(df, 10)).trades.iloc[0]
    assert t.reason == "stop" and t.exit_px == pytest.approx(95.0 * 0.999) and t.r < 0


def test_gap_through_stop_fills_at_open_not_stop():
    df = flat_frame()
    set_bar(df, 13, o=90.0, h=91.0, l=89.0, c=90.5)
    t = run(df, sig(df, 10)).trades.iloc[0]
    assert t.reason == "stop_gap" and t.exit_px == pytest.approx(90.0 * 0.999) and t.exit_px < 95.0


def test_stop_and_target_same_bar_assumes_stop_first():
    df = flat_frame()
    set_bar(df, 13, o=100.0, h=112.0, l=94.0, c=105.0)
    assert run(df, sig(df, 10)).trades.iloc[0].reason == "stop"


def test_target_hit_and_gap_above_target():
    df = flat_frame()
    set_bar(df, 13, o=100.0, h=111.0, l=99.5, c=110.0)
    t = run(df, sig(df, 10)).trades.iloc[0]
    assert t.reason == "target" and t.exit_px == pytest.approx(110.0) and t.r > 0
    df = flat_frame()
    set_bar(df, 13, o=115.0, h=116.0, l=114.0, c=115.0)
    assert run(df, sig(df, 10)).trades.iloc[0].exit_px == pytest.approx(115.0)


def test_time_exit_after_max_hold_bars():
    df = flat_frame()
    t = run(df, sig(df, 10)).trades.iloc[0]
    assert t.reason == "time" and t.bars == 5 and t.exit_date == df.index[16]


def test_open_position_closed_at_period_end():
    df = flat_frame(20)
    cfg = BTConfig(strategy="swing", max_hold_bars=50, max_position_pct=1.0, max_total_risk_pct=0.5, resize_to_limits=False)
    r = simulate(sig(df, 15), {"AAA": df}, flat_frame(20), cfg)
    assert r.trades.iloc[0].reason == "end_of_period" and r.trades.iloc[0].exit_date == df.index[-1]


def test_gap_that_invalidates_signal_is_skipped():
    df = flat_frame()
    set_bar(df, 11, o=94.0, h=95.0, l=93.0, c=94.0)                   # opens below the stop
    r = run(df, sig(df, 10))
    assert r.trades.empty and r.rejected["gap_invalidated"] == 1


def test_max_positions_and_new_per_day_respected():
    syms = {f"S{i}": flat_frame() for i in range(8)}
    spy = flat_frame()
    rows = [sig(syms[s], 10, sym=s, score=90 - i)["symbol"].iloc[0] for i, s in enumerate(syms)]
    signals = pd.concat([sig(syms[s], 10, sym=s, score=90 - i) for i, s in enumerate(syms)], ignore_index=True)
    cfg = BTConfig(strategy="swing", max_hold_bars=30, max_positions=3, max_new_per_day=2, max_position_pct=1.0,
                   max_total_risk_pct=0.5, max_sector_pct=1.0, resize_to_limits=False)
    r = simulate(signals, syms, spy, cfg, {})
    assert len(r.trades) == 2                                         # day limit of 2 new entries on the signal day
    assert set(r.trades.symbol) == {"S0", "S1"}                        # highest scores first


def test_risk_engine_rejection_is_counted_and_resize_option_works():
    df = flat_frame()
    tight = sig(df, 10, stop=99.5, target=105.0)                       # 0.5 stop => huge position, breaks 20 % cap
    cfg_rej = BTConfig(strategy="swing", max_hold_bars=5, resize_to_limits=False, slippage_bps=0)
    r = run(df, tight, cfg_rej)
    assert r.trades.empty and r.rejected["rk_position_size"] >= 1
    cfg_res = BTConfig(strategy="swing", max_hold_bars=5, resize_to_limits=True, slippage_bps=0)
    r2 = run(df, tight, cfg_res)
    assert len(r2.trades) == 1 and r2.rejected["resized_to_limits"] == 1
    assert r2.trades.iloc[0].shares * 100.0 <= 0.2 * 100_000 + 1e-6


def test_equity_curve_tracks_cash_and_positions():
    df = flat_frame()
    r = run(df, sig(df, 10))
    assert len(r.equity) == N and r.equity.iloc[0] == 100_000
    assert r.equity.iloc[-1] == pytest.approx(100_000 + r.trades.pnl.sum())
    assert (r.exposure >= 0).all() and (r.exposure <= 1).all()


def test_threshold_filters_signals():
    df = flat_frame()
    s = sig(df, 10, score=65.0)
    assert run(df, s, BTConfig(strategy="swing", threshold=70.0, max_hold_bars=5)).trades.empty
    assert len(run(df, s, BTConfig(strategy="swing", threshold=60.0, max_hold_bars=5, max_position_pct=1.0,
                                   max_total_risk_pct=0.5, resize_to_limits=True)).trades) == 1


# ---------------------------------------------------------------- no look-ahead in signal generation
def test_signals_do_not_change_when_future_data_changes():
    stock, spy = trend(520, drift=0.001, seed=31), trend(520, drift=0.0003, seed=32)
    cfg = BTConfig(strategy="swing", warmup=220, eval_step=1)
    full = build_signals({"AAA": stock}, spy, {}, {}, ScanCfg(), cfg)
    cut = 420
    part = build_signals({"AAA": stock.iloc[:cut]}, spy.iloc[:cut], {}, {}, ScanCfg(), cfg)
    cutoff = stock.index[cut - 1]
    a = full[full.date <= cutoff].reset_index(drop=True)
    b = part.reset_index(drop=True)
    assert len(a) == len(b) and len(a) > 0
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9)
    # perturb strictly-future bars and re-check
    mutated = stock.copy()
    mutated.iloc[cut:, :4] *= 1.7
    again = build_signals({"AAA": mutated}, spy, {}, {}, ScanCfg(), cfg)
    pd.testing.assert_frame_equal(a, again[again.date <= cutoff].reset_index(drop=True), check_exact=False, rtol=1e-9)


def test_longterm_signals_generated_on_uptrend():
    stock, spy = trend(700, drift=0.0012, vol=0.008, seed=33, volume=3e6), trend(700, drift=0.0003, seed=34)
    cfg = BTConfig(strategy="longterm")
    sig_ = build_signals({"AAA": stock}, spy, {}, {}, ScanCfg(), cfg)
    assert len(sig_) > 0 and (sig_.date.map(lambda d: stock.index.get_loc(d)) % 5 == 0).all()


# ---------------------------------------------------------------- metrics
def test_stats_values_and_guards():
    tr = pd.DataFrame({"pnl": [100, -50, 200, -50], "ret_pct": [0.05, -0.02, 0.08, -0.02], "r": [1.0, -1.0, 2.0, -1.0], "bars": [3, 2, 5, 4]})
    eq = pd.Series([100, 101, 99, 103, 102.0], index=pd.date_range("2026-01-01", periods=5))
    s = M.compute_stats(tr, eq)
    assert s.win_rate == 0.5 and s.expectancy_r == pytest.approx(0.25) and s.profit_factor == pytest.approx(300 / 100)
    assert s.max_drawdown == pytest.approx(99 / 101 - 1) and s.total_return == pytest.approx(0.02)
    assert s.sharpe is None and s.cagr is None and s.meaningful is False
    assert any("NOT statistically meaningful" in n for n in s.notes)
    lo, hi = s.win_rate_ci
    assert 0 < lo < 0.5 < hi < 1


def test_sharpe_reported_only_with_enough_data():
    n = 300
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(0)
    eq = pd.Series(100_000 * np.cumprod(1 + 0.0004 + 0.01 * rng.standard_normal(n)), index=idx)
    tr = pd.DataFrame({"pnl": rng.normal(10, 100, 40), "ret_pct": rng.normal(0.001, 0.02, 40), "r": rng.normal(0.1, 1, 40), "bars": 5})
    s = M.compute_stats(tr, eq)
    assert s.sharpe is not None and s.meaningful and s.cagr is not None


def test_wilson_and_buyhold():
    lo, hi = M.wilson_interval(5, 10)
    assert lo < 0.5 < hi and M.wilson_interval(0, 0) == (0.0, 1.0)
    c = pd.Series([100, 110, 99, 120.0], index=pd.date_range("2026-01-01", periods=4))
    bh = M.buy_hold(c, c.index[0], c.index[-1])
    assert bh["total_return"] == pytest.approx(0.2) and bh["max_drawdown"] == pytest.approx(99 / 110 - 1)
    assert "SURVIVORSHIP" in " ".join(M.DISCLOSURES)


# ---------------------------------------------------------------- walk-forward
def test_walk_forward_structure_and_oos_only():
    n = 900
    spy = trend(n, seed=41)
    frames = {"AAA": trend(n, seed=42), "BBB": trend(n, seed=43)}
    rng = np.random.default_rng(1)
    rows = []
    for s in frames:
        for i in range(230, n - 15, 7):
            px = float(frames[s].close.iloc[i])
            rows.append({"symbol": s, "date": frames[s].index[i], "score": float(rng.uniform(60, 95)), "stop": px * 0.96,
                         "target": px * 1.08, "rr": 2.0, "atr": px * 0.02, "close": px})
    signals = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    cfg = BTConfig(strategy="swing", resize_to_limits=True)
    wf = walk_forward(signals, frames, spy, cfg, train_days=300, test_days=150, thresholds=(60, 75, 90))
    assert len(wf.windows) >= 2
    prev_end = None
    for w in wf.windows:
        assert w.train[1] < w.test[0]                                        # test strictly after train
        if prev_end:
            assert w.test[0] > prev_end                                      # OOS windows do not overlap
        prev_end = w.test[1]
        assert w.chosen_threshold in (60, 75, 90, 70.0)
        assert (w.oos.trades.entry_date >= pd.Timestamp(w.test[0])).all()    # nothing from the training period leaks in
    assert wf.stats.n_trades == sum(len(w.oos.trades) for w in wf.windows)
    assert wf.oos_equity.index.is_monotonic_increasing and wf.oos_equity.iloc[0] == pytest.approx(100_000, rel=0.05)
    assert any("window end" in n for n in wf.stats.notes)


def test_walk_forward_needs_enough_history():
    spy = trend(200, seed=1)
    with pytest.raises(ValueError):
        walk_forward(pd.DataFrame(columns=["symbol", "date", "score", "stop", "target", "rr", "atr", "close"]), {"A": spy}, spy,
                     BTConfig(), train_days=300, test_days=100)
