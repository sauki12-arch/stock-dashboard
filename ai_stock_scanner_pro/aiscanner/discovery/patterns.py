"""Pattern detectors with explicit, testable rules.

CORE detectors are vectorised boolean columns computed from the indicator frame; they feed
the score. EXPERIMENTAL detectors (triangle / wedge / cup-and-handle) work on a price window,
are reported separately and **never add points to the score** until validated by backtests.

Pivot points are confirmed `k` bars after they occur, so a pivot is only used once its
confirming bars exist (no look-ahead at the last bar).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

CORE = ["breakout", "near_breakout", "tight_consolidation", "pullback_support", "sma20_reclaim",
        "volatility_contraction", "new_high", "lt_pullback", "reclaim_sma50"]
EXPERIMENTAL = ["triangle_ascending", "triangle_descending", "triangle_symmetrical",
                "wedge_rising", "wedge_falling", "cup_and_handle"]


def core_flags(f: pd.DataFrame) -> pd.DataFrame:
    """Vectorised, causal. Rules (all on completed daily bars):

    breakout            close > prior 20-bar high, rel.volume >= 1.5, close in top 30 % of the bar's range, close > SMA50
    near_breakout       close within 1.5 % below the prior 20-bar high, SMA20 > SMA50, close > SMA50
    tight_consolidation 10-bar high-low range <= 2.5 ATR and 10-bar close std < 1.5 % of price, close > SMA50
    pullback_support    uptrend (close > SMA50 > SMA200), low <= EMA20 + 0.5 ATR, close >= SMA50, RSI 35..55, 5-day return < 0
    sma20_reclaim       close > SMA20 after >= 3 of the previous 5 closes were below it, SMA20 not falling > 1 %/5d
    volatility_contraction  10/50-day mean true-range ratio < 0.75 or Bollinger width in lowest 20 % of 120 bars; close > SMA50
    new_high            close >= 98 % of the 252-day high with relative volume >= 1.0 and close > SMA50 > SMA200   (long-term)
    lt_pullback         close > SMA200 with SMA50 > SMA200, low within 1 ATR of SMA50, RSI 35..55                   (long-term)
    reclaim_sma50       close > SMA50 after >= 3 of the previous 5 closes below it, SMA200 rising                  (long-term)
    """
    c, atr = f["close"], f["atr14"]
    up50 = c > f["sma50"]
    out = pd.DataFrame(index=f.index)
    out["breakout"] = (c > f["high20_prev"]) & (f["relvol"] >= 1.5) & (f["close_loc"] >= 0.7) & up50
    out["near_breakout"] = (c <= f["high20_prev"]) & (c >= f["high20_prev"] * 0.985) & (f["sma20"] > f["sma50"]) & up50
    out["tight_consolidation"] = (f["range10"] <= 2.5 * atr) & (f["std10_pct"] < 0.015) & up50
    uptrend = (c > f["sma50"]) & (f["sma50"] > f["sma200"])
    out["pullback_support"] = (uptrend & (f["low"] <= f["ema20"] + 0.5 * atr) & (c >= f["sma50"])
                               & f["rsi14"].between(35, 55) & (f["ret5"] < 0))
    below20 = (c < f["sma20"]).astype(float)
    out["sma20_reclaim"] = (c > f["sma20"]) & (below20.shift(1).rolling(5).sum() >= 3) & (f["sma20_chg5"] > -0.01)
    out["volatility_contraction"] = ((f["tr_ratio_10_50"] < 0.75) | (f["bb_width_pctile"] <= 0.2)) & up50
    out["new_high"] = (c >= 0.98 * f["high252"]) & (f["relvol"] >= 1.0) & uptrend
    out["lt_pullback"] = ((c > f["sma200"]) & (f["sma50"] > f["sma200"]) & ((f["low"] - f["sma50"]).abs() <= atr)
                          & f["rsi14"].between(35, 55))
    below50 = (c < f["sma50"]).astype(float)
    out["reclaim_sma50"] = (up50 & (below50.shift(1).rolling(5).sum() >= 3) & (f["sma200_chg20"] > 0))
    return out.fillna(False).astype(bool)


# ---------------------------------------------------------------- pivots
def find_pivots(high: np.ndarray, low: np.ndarray, k: int = 3):
    """Return (idx_high, idx_low) of confirmed pivots (extreme within +-k bars; confirmed k bars later)."""
    n = len(high)
    ph, pl = [], []
    for i in range(k, n - k):
        if high[i] == high[i - k:i + k + 1].max() and high[i] > high[i - 1]:
            ph.append(i)
        if low[i] == low[i - k:i + k + 1].min() and low[i] < low[i - 1]:
            pl.append(i)
    return np.array(ph, dtype=int), np.array(pl, dtype=int)


def nearest_levels(df: pd.DataFrame, close: float, lookback_res: int = 120, lookback_sup: int = 60, k: int = 3):
    """(support, resistance) from confirmed pivots; None if absent."""
    h, l = df["high"].to_numpy()[-lookback_res:], df["low"].to_numpy()[-lookback_res:]
    ph, pl = find_pivots(h, l, k)
    res = [h[i] for i in ph if h[i] > close]
    off = len(l) - lookback_sup
    sup = [l[i] for i in pl if i >= off and l[i] < close]
    return (max(sup) if sup else None), (min(res) if res else None)


@dataclass
class PatternHit:
    name: str
    experimental: bool = True
    details: dict = field(default_factory=dict)


def _line(idx: np.ndarray, vals: np.ndarray):
    slope, icpt = np.polyfit(idx.astype(float), vals, 1)
    return slope, icpt


def _trendlines(df: pd.DataFrame, window: int, k: int = 2):
    w = df.iloc[-window:]
    h, l = w["high"].to_numpy(), w["low"].to_numpy()
    ph, pl = find_pivots(h, l, k)
    if len(ph) < 2 or len(pl) < 2 or len(ph) + len(pl) < 5:
        return None
    mean_p = float(w["close"].mean())
    sh, ih = _line(ph, h[ph])
    sl, il = _line(pl, l[pl])
    n = len(w)
    gap0 = (sh * 0 + ih) - (sl * 0 + il)
    gap1 = (sh * (n - 1) + ih) - (sl * (n - 1) + il)
    return {"sh": sh / mean_p, "sl": sl / mean_p, "gap0": gap0, "gap1": gap1, "n": n,
            "upper_end": sh * (n - 1) + ih, "lower_end": sl * (n - 1) + il, "touches": len(ph) + len(pl)}


def detect_triangle(df: pd.DataFrame, window: int = 60) -> PatternHit | None:
    """EXPERIMENTAL. >=2 confirmed pivot highs and lows (>=5 touches) in the window; trendlines converge
    (final gap < 70 % of initial gap, gap1 > 0) and the last close sits inside the lines (+-0.5 ATR slack).
    ascending: upper slope |s|<0.04 %/bar, lower slope > 0.08 %/bar; descending: mirror;
    symmetrical: upper < -0.05 %/bar and lower > +0.05 %/bar."""
    if len(df) < window:
        return None
    t = _trendlines(df, window)
    if not t or t["gap0"] <= 0 or t["gap1"] <= 0 or t["gap1"] >= 0.7 * t["gap0"]:
        return None
    last = float(df["close"].iloc[-1])
    slack = 0.5 * float(df["high"].sub(df["low"]).iloc[-14:].mean())
    if not (t["lower_end"] - slack <= last <= t["upper_end"] + slack):
        return None
    sh, sl = t["sh"], t["sl"]
    if abs(sh) < 0.0004 and sl > 0.0008:
        name = "triangle_ascending"
    elif abs(sl) < 0.0004 and sh < -0.0008:
        name = "triangle_descending"
    elif sh < -0.0005 and sl > 0.0005:
        name = "triangle_symmetrical"
    else:
        return None
    return PatternHit(name, True, {"upper_slope": sh, "lower_slope": sl, "touches": t["touches"],
                                   "upper": t["upper_end"], "lower": t["lower_end"]})


def detect_wedge(df: pd.DataFrame, window: int = 60) -> PatternHit | None:
    """EXPERIMENTAL. Both trendlines slope the same way (|slope| > 0.05 %/bar) and converge
    (final gap < 75 % of initial). rising: lower slope > upper slope > 0; falling: upper slope < lower slope < 0."""
    if len(df) < window:
        return None
    t = _trendlines(df, window)
    if not t or t["gap0"] <= 0 or t["gap1"] <= 0 or t["gap1"] >= 0.75 * t["gap0"]:
        return None
    sh, sl = t["sh"], t["sl"]
    if sh > 0.0005 and sl > sh:
        name = "wedge_rising"
    elif sl < -0.0005 and sh < sl:
        name = "wedge_falling"
    else:
        return None
    return PatternHit(name, True, {"upper_slope": sh, "lower_slope": sl, "touches": t["touches"]})


def detect_cup_handle(df: pd.DataFrame, window: int = 120) -> PatternHit | None:
    """EXPERIMENTAL. Over the last `window` bars: left rim = highest high in the first 40 %; cup bottom = lowest low
    after it with depth 12-35 % of the rim, >= 30 bars from rim to bottom-to-right-rim span, U-shaped (>= 40 % of cup
    bars within the lowest third of the depth; a linear V has 33 %, a parabola 58 %); right rim within 5 % below to 2 % above the left rim; handle =
    last 5-25 bars with a 3-12 % pullback from the right rim, lower average volume than the cup, close in upper half
    of the handle range."""
    if len(df) < window:
        return None
    w = df.iloc[-window:]
    h, l, c, v = (w[x].to_numpy() for x in ("high", "low", "close", "volume"))
    n = len(w)
    li = int(np.argmax(h[: int(n * 0.4)]))
    left = h[li]
    bi = li + int(np.argmin(l[li:]))
    bottom = l[bi]
    depth = (left - bottom) / left
    if not (0.12 <= depth <= 0.35) or bi - li < 15:
        return None
    ri = bi + int(np.argmax(h[bi:]))
    right = h[ri]
    if ri - li < 30 or not (0.95 * left <= right <= 1.02 * left):
        return None
    cup_l = l[li:ri + 1]
    if (cup_l <= bottom + depth * left / 3).mean() < 0.40:
        return None
    hlen = n - 1 - ri
    if not (5 <= hlen <= 25):
        return None
    hl, hh = l[ri:].min(), right
    pull = (right - hl) / right
    if not (0.03 <= pull <= 0.12) or v[ri:].mean() >= v[li:ri + 1].mean():
        return None
    if c[-1] < hl + 0.5 * (hh - hl):
        return None
    return PatternHit("cup_and_handle", True, {"left_rim": float(left), "bottom": float(bottom), "depth": float(depth),
                                               "handle_pullback": float(pull), "pivot": float(right)})


def detect_experimental(df: pd.DataFrame) -> list[PatternHit]:
    hits = []
    for fn in (detect_triangle, detect_wedge, detect_cup_handle):
        try:
            h = fn(df)
        except Exception:  # noqa: BLE001 - experimental code must never break a scan
            h = None
        if h:
            hits.append(h)
    return hits
