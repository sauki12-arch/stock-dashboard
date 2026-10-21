"""Yahoo Finance adapter (via the unofficial `yfinance` package).

Caveats – read before relying on it:
* Unofficial, undocumented, can change or break without notice; no SLA.
* Terms of use restrict it to personal use; do NOT redistribute the data.
* Daily data is often delayed/provisional shortly after the close; intraday history is
  limited (1m: 7 days, 5-30m: 60 days, 60m: 730 days) and is NOT a real-time feed.
* Rate limiting (HTTP 429) is common for large universes: the engine batches, throttles
  and backs off, but cannot guarantee delivery.
The adapter imports `yfinance` lazily so the rest of the app works without it.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import pandas as pd

from .provider import (INTRADAY_MAX_DAYS, OHLCV, DataProvider, ProviderError, ProviderUnavailable,
                       empty_frame, normalize_frame)


class YahooProvider(DataProvider):
    name = "Yahoo Finance (yfinance)"
    is_synthetic = False
    supports_intraday = True
    disclaimer = "נתוני Yahoo Finance – ללא אחריות, עשויים להיות מושהים/חלקיים, לשימוש אישי בלבד"

    def __init__(self, downloader: Callable[..., pd.DataFrame] | None = None):
        self._downloader = downloader

    def _download(self, **kw) -> pd.DataFrame:
        if self._downloader is not None:
            return self._downloader(**kw)
        try:
            import yfinance as yf
        except ImportError as e:  # pragma: no cover
            raise ProviderUnavailable("yfinance is not installed") from e
        try:
            return yf.download(progress=False, threads=False, auto_adjust=True, actions=False, **kw)
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if "RateLimit" in name or "429" in str(e):
                raise ProviderUnavailable(f"Yahoo rate limit: {e}") from e
            raise ProviderUnavailable(f"{name}: {e}") from e

    @staticmethod
    def _extract(raw: pd.DataFrame, sym: str, single: bool) -> pd.DataFrame:
        if raw is None or raw.empty:
            return empty_frame()
        cols = raw.columns
        if isinstance(cols, pd.MultiIndex):
            if sym in cols.get_level_values(0):
                sub = raw[sym]
            elif sym in cols.get_level_values(1):
                sub = raw.xs(sym, axis=1, level=1)
            else:
                return empty_frame()
        elif single:
            sub = raw
        else:
            return empty_frame()
        sub = sub.rename(columns=str.lower)
        if not set(OHLCV) <= set(sub.columns):
            return empty_frame()
        sub = sub[OHLCV].dropna(subset=["close"])
        if sub.empty:
            return empty_frame()
        idx = pd.DatetimeIndex(sub.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        sub.index = idx.normalize()
        return normalize_frame(sub)

    def fetch_daily(self, symbols, start: date, end: date):
        if not symbols:
            return {}
        raw = self._download(tickers=list(symbols), start=start.isoformat(),
                             end=(end + timedelta(days=1)).isoformat(), interval="1d", group_by="ticker")
        out: dict[str, pd.DataFrame | ProviderError] = {}
        for s in symbols:
            df = self._extract(raw, s, single=len(symbols) == 1)
            out[s] = df if len(df) else ProviderError(s, "Yahoo returned no data (delisted, wrong symbol or throttled)")
        return out

    def fetch_intraday(self, symbol, interval, days):
        if interval not in INTRADAY_MAX_DAYS:
            return ProviderError(symbol, f"unsupported interval {interval}", retryable=False)
        days = min(days, INTRADAY_MAX_DAYS[interval])
        raw = self._download(tickers=symbol, period=f"{days}d", interval=interval, prepost=False, group_by="ticker")
        if raw is None or raw.empty:
            return ProviderError(symbol, "Yahoo returned no intraday data")
        df = raw
        if isinstance(df.columns, pd.MultiIndex):
            df = df[symbol] if symbol in df.columns.get_level_values(0) else df.droplevel(0, axis=1)
        df = df.rename(columns=str.lower)[OHLCV].dropna(subset=["close"])
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_convert("UTC").tz_localize(None) if idx.tz is not None else idx
        df.index = idx
        return normalize_frame(df)

    def health_check(self):
        try:
            res = self.fetch_daily(["SPY"], date.today() - timedelta(days=10), date.today())
            r = res["SPY"]
            return (not isinstance(r, ProviderError), "ok" if not isinstance(r, ProviderError) else str(r))
        except ProviderUnavailable as e:
            return False, str(e)
