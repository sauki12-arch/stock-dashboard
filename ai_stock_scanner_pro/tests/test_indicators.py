import numpy as np
import pandas as pd
import pytest

from aiscanner.discovery import indicators as I
from aiscanner.discovery.indicators import build_frame
from tests.helpers import mk, trend


def test_sma_ema_known_values():
    s = pd.Series([1, 2, 3, 4, 5, 6.0])
    assert I.sma(s, 3).tolist()[2:] == [2, 3, 4, 5]
    assert np.isnan(I.sma(s, 3).iloc[1])
    e = I.ema(pd.Series([1.0] * 10), 5)
    assert e.dropna().eq(1.0).all()


def test_rsi_extremes_and_midpoint():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert I.rsi(up).iloc[-1] == pytest.approx(100.0)
    dn = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert I.rsi(dn).iloc[-1] == pytest.approx(0.0, abs=1e-9)
    alt = pd.Series([100.0 + (i % 2) for i in range(200)])
    assert 45 < I.rsi(alt).iloc[-1] < 55


def test_rsi_matches_loop_reference_wilder():
    r = np.random.default_rng(3)
    close = pd.Series(100 + np.cumsum(r.standard_normal(200)))
    n = 14
    d = close.diff().to_numpy()
    ag = al = np.nan
    ref = np.full(len(close), np.nan)
    for i in range(1, len(close)):
        g, l = max(d[i], 0.0), max(-d[i], 0.0)
        ag = g if np.isnan(ag) else (ag * (n - 1) + g) / n
        al = l if np.isnan(al) else (al * (n - 1) + l) / n
        if i >= n:
            ref[i] = 100 - 100 / (1 + ag / al)
    got = I.rsi(close, n).to_numpy()
    assert np.allclose(got[n + 1:], ref[n + 1:], atol=1e-6)      # same recursion, after warm-up


def test_atr_constant_range():
    df = mk(np.full(60, 100.0), hi=np.full(60, 102.0), lo=np.full(60, 98.0))
    assert I.atr(df).iloc[-1] == pytest.approx(4.0, rel=1e-6)


def test_macd_zero_on_flat():
    line, sig, hist = I.macd(pd.Series(np.full(100, 50.0)))
    assert line.dropna().abs().max() < 1e-9 and hist.dropna().abs().max() < 1e-9


def test_bollinger_flat_zero_width():
    mid, up, lo = I.bollinger(pd.Series(np.full(40, 10.0)))
    assert (up - lo).dropna().abs().max() < 1e-12


def test_obv_direction():
    df = mk([10, 11, 12, 11, 12], vol=[100, 100, 100, 50, 70])
    assert I.obv(df).tolist() == [0, 100, 200, 150, 220]


def test_frame_has_expected_columns_and_values():
    df = trend(400)
    f = build_frame(df)
    for col in ["sma20", "sma50", "sma200", "ema20", "ema50", "ema200", "rsi14", "macd", "macd_signal", "macd_hist",
                "atr14", "atr_pct", "bb_up", "bb_lo", "relvol", "obv", "dollar_vol20", "wk_align", "ret252",
                "rs63_spy", "high20_prev", "persist6m"]:
        assert col in f.columns
    assert f["sma50"].iloc[-1] == pytest.approx(df.close.tail(50).mean())
    # relvol uses the PRIOR 20-day mean (today's volume must not dilute its own baseline)
    assert f["relvol"].iloc[-1] == pytest.approx(df.volume.iloc[-1] / df.volume.iloc[-21:-1].mean())
    assert f["rs63_spy"].isna().all()                      # no benchmark supplied


def test_relative_strength_vs_benchmark():
    stock, spy = trend(300, drift=0.002, seed=2), trend(300, drift=0.0, seed=3)
    f = build_frame(stock, spy)
    manual = stock.close.pct_change(63).iloc[-1] - spy.close.pct_change(63).iloc[-1]
    assert f["rs63_spy"].iloc[-1] == pytest.approx(manual)


def test_frame_is_causal_no_lookahead():
    """Row t of a full-history frame must equal the last row of a frame built from data ≤ t only."""
    df = trend(420, seed=5)
    spy = trend(420, seed=6)
    full = build_frame(df, spy, spy)
    for cut in (260, 300, 359, 419):
        part = build_frame(df.iloc[:cut + 1], spy.iloc[:cut + 1], spy.iloc[:cut + 1])
        a, b = full.iloc[cut], part.iloc[-1]
        pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-9, atol=1e-9)


def test_weekly_alignment_uses_only_past_weeks():
    df = trend(300, seed=9)
    f = build_frame(df)
    t = 200
    mutated = df.copy()
    mutated.iloc[t + 1:, mutated.columns.get_loc("close")] *= 3.0        # change the future
    f2 = build_frame(mutated)
    assert f.iloc[t]["wk_sma_fast"] == pytest.approx(f2.iloc[t]["wk_sma_fast"])
    assert f.iloc[t]["wk_sma_slow"] == pytest.approx(f2.iloc[t]["wk_sma_slow"])
