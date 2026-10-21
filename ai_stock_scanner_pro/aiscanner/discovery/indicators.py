"""Causal technical indicators. Every column at row t uses data up to and including t only.

`build_frame(df, spy, sector)` returns one wide DataFrame so the same numbers feed the live
scanner and the backtester (which reads row t and then trades at t+1).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI."""
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn
    out = 100 - 100 / (1 + rs)
    out = out.where(dn != 0, 100.0)
    return out.where(~(up.isna() | dn.isna()))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(close, fast) - ema(close, slow)
    signal = line.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return line, signal, line - signal


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ATR."""
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def obv(df: pd.DataFrame) -> pd.Series:
    return (np.sign(df["close"].diff().fillna(0)) * df["volume"]).cumsum()


def _weekly_context(close: pd.Series, fast: int = 10, slow: int = 30) -> pd.DataFrame:
    """Weekly SMA(fast/slow) where the *current* week uses today's close and the previous n-1 weeks are
    completed ones – so row t never sees data after t. (Weeks are Mon-Fri groups of trading days.)"""
    codes = pd.factorize(close.index.to_period("W-FRI"))[0]        # monotone week id per row
    weekly_last = close.groupby(codes).last().to_numpy()
    out = pd.DataFrame(index=close.index, dtype="float64")
    for n, name in ((fast, "wk_sma_fast"), (slow, "wk_sma_slow")):
        roll = pd.Series(weekly_last).rolling(n - 1, min_periods=n - 1).sum().to_numpy()
        prev = np.r_[np.nan, roll][codes]                           # rolling sum ending at the previous week
        out[name] = (prev + close.to_numpy()) / n
    return out


def spread_free_liquidity(df: pd.DataFrame) -> pd.Series:
    return (df["close"] * df["volume"]).rolling(20, min_periods=20).mean()


def build_frame(df: pd.DataFrame, spy: pd.DataFrame | None = None,
                sector: pd.DataFrame | None = None) -> pd.DataFrame:
    f: dict[str, pd.Series] = {}          # assembled into one DataFrame at the end (much faster than setitem)
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    f["open"], f["high"], f["low"], f["close"], f["volume"] = df["open"], h, l, c, v
    for n in (20, 50, 100, 200):
        f[f"sma{n}"] = sma(c, n)
    for n in (20, 50, 200):
        f[f"ema{n}"] = ema(c, n)
    f["sma20_chg5"] = f["sma20"] / f["sma20"].shift(5) - 1
    f["sma50_chg10"] = f["sma50"] / f["sma50"].shift(10) - 1
    f["sma200_chg20"] = f["sma200"] / f["sma200"].shift(20) - 1

    f["rsi14"] = rsi(c)
    f["macd"], f["macd_signal"], f["macd_hist"] = macd(c)
    f["macd_hist_chg3"] = (f["macd_hist"] - f["macd_hist"].shift(3)) / c
    roc10 = c.pct_change(10)
    f["roc10"] = roc10
    f["mom_accel"] = roc10 - roc10.shift(5)

    f["tr"] = true_range(df)
    f["atr14"] = atr(df)
    f["atr_pct"] = f["atr14"] / c
    f["tr_ratio_10_50"] = f["tr"].rolling(10).mean() / f["tr"].rolling(50).mean()
    f["bb_mid"], f["bb_up"], f["bb_lo"] = bollinger(c)
    f["bb_width"] = (f["bb_up"] - f["bb_lo"]) / f["bb_mid"]
    f["bb_width_pctile"] = f["bb_width"].rolling(120, min_periods=60).rank(pct=True)
    f["pct_b"] = (c - f["bb_lo"]) / (f["bb_up"] - f["bb_lo"])

    f["vol_avg20"] = v.rolling(20, min_periods=20).mean()
    f["relvol"] = v / v.rolling(20, min_periods=20).mean().shift(1)
    f["dollar_vol20"] = spread_free_liquidity(df)
    o = obv(df)
    f["obv"] = o
    f["obv_trend20"] = (o - o.shift(20)) / (f["vol_avg20"] * 20)
    f["obv_trend60"] = (o - o.shift(60)) / (v.rolling(60, min_periods=60).mean() * 60)
    up = np.where(c.diff() > 0, v, 0.0)
    dn = np.where(c.diff() < 0, v, 0.0)
    f["updown20"] = pd.Series(up, index=c.index).rolling(20).sum() / pd.Series(dn, index=c.index).rolling(20).sum().replace(0, np.nan)
    f["updown60"] = pd.Series(up, index=c.index).rolling(60).sum() / pd.Series(dn, index=c.index).rolling(60).sum().replace(0, np.nan)

    f["high20_prev"] = h.rolling(20, min_periods=20).max().shift(1)
    f["high50_prev"] = h.rolling(50, min_periods=50).max().shift(1)
    f["low50_prev"] = l.rolling(50, min_periods=50).min().shift(1)
    f["high252"] = h.rolling(252, min_periods=120).max()
    f["high252_prev"] = f["high252"].shift(1)
    f["low10"] = l.rolling(10, min_periods=10).min()
    f["low20"] = l.rolling(20, min_periods=20).min()
    f["range10"] = h.rolling(10, min_periods=10).max() - l.rolling(10, min_periods=10).min()
    f["std10_pct"] = c.rolling(10, min_periods=10).std(ddof=0) / c
    f["dist_high252"] = c / f["high252"] - 1
    f["close_loc"] = (c - l) / (h - l).replace(0, np.nan)
    roll_max252 = c.rolling(252, min_periods=60).max()
    f["maxdd126"] = (c / roll_max252 - 1).rolling(126, min_periods=60).min()

    for n in (5, 20, 63, 126, 252):
        f[f"ret{n}"] = c.pct_change(n)
    r1 = c.pct_change()
    f["rv20"] = r1.rolling(20, min_periods=20).std() * np.sqrt(252)
    f["rv60"] = r1.rolling(60, min_periods=60).std() * np.sqrt(252)
    m21 = c.pct_change(21)
    f["persist6m"] = sum((m21.shift(21 * k) > 0).astype(float) for k in range(6)) / 6
    f["persist6m"] = f["persist6m"].where(m21.shift(21 * 5).notna())

    wk = _weekly_context(c)
    f["wk_sma_fast"], f["wk_sma_slow"] = wk["wk_sma_fast"], wk["wk_sma_slow"]
    f["wk_align"] = ((c > f["wk_sma_fast"]) & (f["wk_sma_fast"] > f["wk_sma_slow"])).astype(float)
    f["wk_align"] = f["wk_align"].where(f["wk_sma_slow"].notna())

    for name, bench in (("spy", spy), ("sec", sector)):
        if bench is not None and len(bench):
            bc = bench["close"].reindex(c.index).ffill()
            for n in (20, 63, 126, 252):
                f[f"rs{n}_{name}"] = f[f"ret{n}"] - bc.pct_change(n)
            line = c / bc
            f[f"rsline_{name}"] = line / line.rolling(50, min_periods=50).mean() - 1
        else:
            for n in (20, 63, 126, 252):
                f[f"rs{n}_{name}"] = pd.Series(np.nan, index=c.index)
            f[f"rsline_{name}"] = pd.Series(np.nan, index=c.index)
    return pd.DataFrame(f, index=df.index)
