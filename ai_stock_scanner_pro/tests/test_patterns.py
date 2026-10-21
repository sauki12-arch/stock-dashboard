import numpy as np
import pandas as pd

from aiscanner.discovery.indicators import build_frame
from aiscanner.discovery.patterns import (core_flags, detect_cup_handle, detect_experimental, detect_triangle,
                                          detect_wedge, find_pivots, nearest_levels)
from tests.helpers import mk, trend


def osc(u, l, period=10):
    t = np.arange(len(u))
    mid, amp = (u + l) / 2, (u - l) / 2
    c = mid + amp * np.sin(2 * np.pi * t / period)
    return c, c + 0.4, c - 0.4


def test_pivots_confirmed_only():
    h = np.array([1, 2, 3, 2, 1, 2, 5, 2, 1.0])
    ph, _ = find_pivots(h, h - 0.5, k=2)
    assert list(ph) == [2, 6]
    ph2, _ = find_pivots(h[:7], (h - 0.5)[:7], k=2)     # last pivot not yet confirmed by 2 later bars
    assert list(ph2) == [2]


def test_symmetrical_triangle():
    t = np.arange(60)
    c, hi, lo = osc(110 - 0.12 * t, 90 + 0.12 * t)
    hit = detect_triangle(mk(c, hi=hi, lo=lo))
    assert hit and hit.name == "triangle_symmetrical" and hit.experimental


def test_ascending_triangle():
    t = np.arange(60)
    c, hi, lo = osc(np.full(60, 110.0), 90 + 0.25 * t)
    hit = detect_triangle(mk(c, hi=hi, lo=lo))
    assert hit and hit.name == "triangle_ascending"


def test_descending_triangle():
    t = np.arange(60)
    c, hi, lo = osc(110 - 0.25 * t, np.full(60, 90.0))
    hit = detect_triangle(mk(c, hi=hi, lo=lo))
    assert hit and hit.name == "triangle_descending"


def test_rising_and_falling_wedge():
    t = np.arange(60)
    c, hi, lo = osc(100 + 0.20 * t, 90 + 0.30 * t)
    hit = detect_wedge(mk(c, hi=hi, lo=lo))
    assert hit and hit.name == "wedge_rising"
    c, hi, lo = osc(150 - 0.30 * t, 140 - 0.20 * t)
    hit = detect_wedge(mk(c, hi=hi, lo=lo))
    assert hit and hit.name == "wedge_falling"


def test_diverging_range_is_not_a_triangle_or_wedge():
    t = np.arange(60)
    c, hi, lo = osc(100 + 0.12 * t, 100 - 0.12 * t)       # expanding (megaphone)
    df = mk(c, hi=hi, lo=lo)
    assert detect_triangle(df) is None and detect_wedge(df) is None


def test_short_history_returns_none():
    assert detect_triangle(mk(np.linspace(1, 2, 30))) is None
    assert detect_cup_handle(mk(np.linspace(1, 2, 60))) is None


def cup_series(handle=True, depth=0.25):
    n = 120
    left = 100.0
    x = np.linspace(-1, 1, 70)
    cup = left * (1 - depth * (1 - x ** 2))                 # parabola: U shape
    pre = np.linspace(92, left, 15)
    rise = np.linspace(cup[-1], left * 0.99, 15) if False else np.array([])
    h = np.linspace(left * 0.99, left * 0.94, 8) if handle else np.linspace(left * 0.99, left, 8)
    h2 = np.linspace(left * 0.94, left * 0.975, 12) if handle else np.linspace(left, left, 12)
    c = np.r_[pre, cup, h, h2][-n:] if len(pre) + len(cup) + len(h) + len(h2) >= n else \
        np.r_[np.full(n - len(pre) - len(cup) - len(h) - len(h2), 92.0), pre, cup, h, h2]
    hi, lo = c * 1.003, c * 0.997
    vol = np.r_[np.full(n - 20, 2e6), np.full(20, 1e6)]      # quieter handle
    return mk(c, hi=hi, lo=lo, vol=vol)


def test_cup_and_handle_detected():
    hit = detect_cup_handle(cup_series())
    assert hit and hit.name == "cup_and_handle" and 0.12 <= hit.details["depth"] <= 0.35


def test_cup_without_handle_pullback_rejected():
    assert detect_cup_handle(cup_series(handle=False)) is None


def test_v_shape_rejected():
    n = 120
    c = np.r_[np.linspace(100, 75, 35), np.linspace(75, 99, 35), np.linspace(99, 94, 8), np.linspace(94, 97.5, 12)]
    c = np.r_[np.full(n - len(c), 95.0), c]
    df = mk(c, hi=c * 1.003, lo=c * 0.997, vol=np.r_[np.full(n - 20, 2e6), np.full(20, 1e6)])
    assert detect_cup_handle(df) is None


def test_experimental_never_raises_on_garbage():
    df = mk(np.full(150, 10.0))
    assert detect_experimental(df) == []


def test_core_flag_breakout_requires_volume_and_trend():
    base = np.r_[np.linspace(60, 100, 250), 100 + 0.2 * np.sin(np.arange(40))]       # uptrend then flat base
    vol = np.full(len(base), 1e6)
    close = np.r_[base, 106.0]
    volume = np.r_[vol, 2.5e6]
    df = mk(close, hi=close * 1.003, lo=close * 0.99, vol=volume)
    f = build_frame(df)
    fl = core_flags(f)
    assert bool(fl["breakout"].iloc[-1])
    volume_low = np.r_[vol, 1.0e6]
    f2 = build_frame(mk(close, hi=close * 1.003, lo=close * 0.99, vol=volume_low))
    assert not bool(core_flags(f2)["breakout"].iloc[-1])


def test_core_flag_pullback_and_reclaim():
    up = np.linspace(50, 120, 300)
    dip = np.linspace(120, 112, 6)
    c = np.r_[up, dip]
    df = mk(c, hi=c * 1.004, lo=c * 0.99)
    # SMA20 reclaim: sell-off below SMA20 for several bars, then close back above
    c2 = np.r_[np.linspace(50, 120, 300), np.linspace(120, 114, 6), [116.0, 119.0]]
    fl2 = core_flags(build_frame(mk(c2, hi=c2 * 1.004, lo=c2 * 0.996)))
    assert bool(fl2["sma20_reclaim"].iloc[-1])


def test_core_flags_are_causal():
    df = trend(420, seed=11)
    full = core_flags(build_frame(df))
    cut = 350
    part = core_flags(build_frame(df.iloc[:cut + 1]))
    assert full.iloc[cut].to_dict() == part.iloc[-1].to_dict()


def test_nearest_levels_pick_pivots():
    c = np.r_[np.linspace(100, 120, 20), np.linspace(120, 105, 15), np.linspace(105, 112, 15)]
    df = mk(c, hi=c * 1.002, lo=c * 0.998)
    sup, res = nearest_levels(df, float(c[-1]))
    assert res is not None and res > c[-1]
    assert sup is None or sup < c[-1]


def test_pullback_flag_fires_on_shallow_dip_not_on_breakdown():
    rng = np.random.default_rng(0)
    up = np.linspace(50, 120, 300) + rng.normal(0, 0.6, 300)
    shallow = np.r_[up, np.linspace(120, 116, 8)]
    deep = np.r_[up, np.linspace(120, 110, 8)]
    f1 = core_flags(build_frame(mk(shallow, hi=shallow * 1.004, lo=shallow * 0.99)))
    f2 = core_flags(build_frame(mk(deep, hi=deep * 1.004, lo=deep * 0.99)))
    assert bool(f1["pullback_support"].iloc[-1]) and not bool(f2["pullback_support"].iloc[-1])
