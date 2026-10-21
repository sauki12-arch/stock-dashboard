"""Deterministic SYNTHETIC market data for tests, demos and offline use.

!! Everything produced here is fabricated by a seeded random process. It is NOT market
!! data and must never be used to make trading decisions. `is_synthetic=True` makes the UI
!! show a permanent warning banner and stamps every scan run as synthetic.
"""
from __future__ import annotations

import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd

from . import market_calendar as cal
from .provider import (INTRADAY_MAX_DAYS, INTRADAY_MINUTES, DataProvider, ProviderError,
                       ProviderUnavailable, empty_frame)

ORIGIN = date(2019, 1, 2)
HORIZON = date(2031, 12, 31)
SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]
INDEX_SYMBOLS = {"SPY", "QQQ", "IWM", "^VIX"}


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


class MockProvider(DataProvider):
    name = "mock (SYNTHETIC)"
    is_synthetic = True
    supports_intraday = True
    disclaimer = "נתונים סינתטיים – אינם נתוני שוק אמיתיים"

    def __init__(self, seed: int = 7, now: Callable[[], datetime] | None = None,
                 sector_map: dict[str, str] | None = None):
        self.seed = seed
        self._now = now or cal.now_utc
        self.sector_map = sector_map or {}
        # failure / quirk injection (used by tests and the diagnostics demo)
        self.fail_symbols: set[str] = set()          # permanent per-symbol error
        self.missing_symbols: set[str] = set()       # provider returns an empty frame
        self.stale_symbols: dict[str, int] = {}      # symbol -> sessions of data withheld
        self.flaky_batches = 0                       # first N fetch_daily calls raise ProviderUnavailable
        self.scale_before: dict[str, tuple[date, float]] = {}  # simulate adjustment revisions
        self.calls: list[tuple[int, date, date]] = []
        self._days = cal.trading_days(ORIGIN, HORIZON)
        self._idx = pd.DatetimeIndex(self._days)
        self._market = self._build_market()

    # ---- synthetic generators ---------------------------------------
    def _build_market(self) -> dict[str, np.ndarray]:
        n = len(self._days)
        rng = np.random.default_rng(_seed("market", self.seed))
        drift = np.empty(n)
        vol = np.empty(n)
        i = 0
        while i < n:                                  # alternating regimes of 60-250 sessions
            ln = int(rng.integers(60, 250))
            kind = rng.choice(["bull", "chop", "bear"], p=[0.5, 0.3, 0.2])
            mu, sg = {"bull": (0.0006, 0.007), "chop": (0.0, 0.009), "bear": (-0.0008, 0.015)}[kind]
            drift[i:i + ln], vol[i:i + ln] = mu, sg
            i += ln
        mkt = drift + vol * rng.standard_normal(n)
        sectors = {s: 0.004 * rng.standard_normal(n) + 0.0001 * rng.standard_normal() for s in SECTOR_ETFS}
        return {"mkt": mkt, "vol": vol, **{f"sec_{k}": v for k, v in sectors.items()}}

    def _sector_of(self, sym: str) -> str:
        if sym in SECTOR_ETFS:
            return sym
        return self.sector_map.get(sym) or SECTOR_ETFS[_seed("sector", sym) % len(SECTOR_ETFS)]

    def _full_series(self, sym: str) -> pd.DataFrame:
        n = len(self._days)
        rng = np.random.default_rng(_seed("sym", sym, self.seed))
        m, v = self._market["mkt"], self._market["vol"]
        if sym == "^VIX":
            rv = pd.Series(m).rolling(20, min_periods=5).std().bfill().to_numpy() * np.sqrt(252) * 100
            close = np.clip(8 + 0.9 * rv + 1.5 * rng.standard_normal(n), 9, 80)
            o = np.r_[close[0], close[:-1]]
            return pd.DataFrame({"open": o, "high": np.maximum(o, close) * 1.02,
                                 "low": np.minimum(o, close) * 0.98, "close": close,
                                 "volume": np.zeros(n)}, index=self._idx)
        sec = self._market[f"sec_{self._sector_of(sym)}"]
        if sym == "SPY":
            beta, sgam, idio_sd, mu = 1.0, 0.0, 0.0008, 0.0
        elif sym == "QQQ":
            beta, sgam, idio_sd, mu = 1.15, 0.0, 0.002, 0.0001
        elif sym == "IWM":
            beta, sgam, idio_sd, mu = 1.1, 0.0, 0.004, 0.0
        elif sym in SECTOR_ETFS:
            beta, sgam, idio_sd, mu = 1.0, 1.0, 0.001, 0.0
        else:
            beta = 0.6 + 1.0 * rng.random()
            sgam = 0.3 + 0.7 * rng.random()
            idio_sd = 0.006 + 0.014 * rng.random()
            mu = rng.normal(0.0002, 0.0004)
        # persistent idiosyncratic trend component => genuine momentum structure to detect
        trend = np.zeros(n)
        i = 0
        while i < n:
            ln = int(rng.integers(30, 160))
            trend[i:i + ln] = rng.normal(0, 0.0012)
            i += ln
        r = beta * m + sgam * sec + trend * (0 if sym in INDEX_SYMBOLS else 1) + mu + idio_sd * rng.standard_normal(n)
        r = np.clip(r, -0.35, 0.35)
        p0 = float(10 ** rng.uniform(1.1, 2.6)) if sym not in INDEX_SYMBOLS and sym not in SECTOR_ETFS else float(rng.uniform(60, 450))
        close = p0 * np.exp(np.cumsum(r))
        gap = idio_sd * 0.3 * rng.standard_normal(n)
        op = np.r_[p0, close[:-1]] * np.exp(gap)
        rng_hl = np.abs(rng.standard_normal(n)) * idio_sd * 0.8 + 0.002 + v * 0.3
        hi = np.maximum(op, close) * (1 + rng_hl * rng.random(n))
        lo = np.minimum(op, close) * (1 - rng_hl * rng.random(n))
        base_vol = float(10 ** rng.uniform(5.6, 7.6))
        volu = base_vol * np.exp(0.35 * rng.standard_normal(n) + 6 * np.abs(r))
        df = pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close, "volume": np.round(volu)},
                          index=self._idx)
        if sym in self.scale_before:
            d0, f = self.scale_before[sym]
            mask = df.index < pd.Timestamp(d0)
            df.loc[mask, ["open", "high", "low", "close"]] *= f
        return df

    # ---- DataProvider API -------------------------------------------
    def fetch_daily(self, symbols, start, end):
        self.calls.append((len(symbols), start, end))
        if self.flaky_batches > 0:
            self.flaky_batches -= 1
            raise ProviderUnavailable("simulated network failure")
        last_final = cal.last_completed_session(self._now(), 20)
        out: dict[str, pd.DataFrame | ProviderError] = {}
        for s in symbols:
            if s in self.fail_symbols:
                out[s] = ProviderError(s, "simulated provider error", retryable=False)
                continue
            if s in self.missing_symbols:
                out[s] = empty_frame()
                continue
            hi = last_final
            lag = self.stale_symbols.get(s, 0)
            for _ in range(lag):
                hi = cal.prev_trading_day(hi)
            df = self._full_series(s)
            lo_ts, hi_ts = pd.Timestamp(start), pd.Timestamp(min(end, hi))
            out[s] = df[(df.index >= lo_ts) & (df.index <= hi_ts)].copy()
        return out

    def fetch_intraday(self, symbol, interval, days):
        if interval not in INTRADAY_MINUTES:
            return ProviderError(symbol, f"unsupported interval {interval}", retryable=False)
        days = min(days, INTRADAY_MAX_DAYS[interval])
        if symbol in self.fail_symbols:
            return ProviderError(symbol, "simulated provider error", retryable=False)
        now = self._now()
        daily = self._full_series(symbol)
        minutes = INTRADAY_MINUTES[interval]
        last_day = cal.to_ny(now).date()
        tds = [d for d in cal.trading_days(last_day - timedelta(days=days + 4), last_day)][-days:]
        rng = np.random.default_rng(_seed("intraday", symbol, self.seed))
        frames = []
        for d in tds:
            row = daily.loc[pd.Timestamp(d)]
            start_dt = cal.session_open(d)
            end_dt = cal.session_close(d)
            n = int((end_dt - start_dt).total_seconds() // 60 // minutes)
            if n <= 0:
                continue
            path = np.linspace(row["open"], row["close"], n + 1) * np.exp(np.cumsum(np.r_[0, 0.0015 * rng.standard_normal(n)]))
            ts = [(start_dt + timedelta(minutes=minutes * k)).astimezone(timezone.utc).replace(tzinfo=None) for k in range(n)]
            o, c = path[:-1], path[1:]
            frames.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0005, "low": np.minimum(o, c) * 0.9995,
                                        "close": c, "volume": np.round(row["volume"] / n * rng.uniform(0.5, 1.5, n))},
                                       index=pd.DatetimeIndex(ts, name="date")))
        if not frames:
            return empty_frame()
        df = pd.concat(frames)
        now_naive = now.astimezone(timezone.utc).replace(tzinfo=None)
        return df[df.index + pd.Timedelta(minutes=minutes) <= now_naive]   # completed bars only

    def health_check(self):
        return True, "synthetic provider (always available)"
