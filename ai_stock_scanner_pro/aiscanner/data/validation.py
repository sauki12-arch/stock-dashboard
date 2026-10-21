"""Data-quality validation. Never repairs silently: every defect becomes an Issue."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import market_calendar as cal

CRITICAL, WARNING = "critical", "warning"


@dataclass
class Issue:
    level: str          # critical | warning
    code: str
    detail: str = ""


@dataclass
class DataQuality:
    symbol: str
    n_bars: int = 0
    last_bar: date | None = None
    sessions_behind: int = 0
    stale: bool = False
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Usable for ranking: no critical issue and not stale."""
        return not self.stale and not any(i.level == CRITICAL for i in self.issues)

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == WARNING]

    def codes(self) -> list[str]:
        return [i.code for i in self.issues] + (["stale"] if self.stale else [])


def drop_incomplete_bars(df: pd.DataFrame, now: datetime, grace_minutes: int = 20) -> pd.DataFrame:
    """Remove daily bars whose session is not complete yet (prevents partial-bar look-ahead)."""
    if df.empty:
        return df
    last_final = cal.last_completed_session(now, grace_minutes)
    return df[df.index.date <= last_final]


def validate_daily(symbol: str, df: pd.DataFrame, now: datetime, *, min_bars: int = 1,
                   max_stale_sessions: int = 1, grace_minutes: int = 20,
                   max_gap_sessions: int = 5, jump_threshold: float = 0.5) -> DataQuality:
    q = DataQuality(symbol=symbol, n_bars=len(df))
    if df.empty:
        q.issues.append(Issue(CRITICAL, "no_data"))
        q.stale = True
        return q
    q.last_bar = df.index[-1].date()
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        q.issues.append(Issue(CRITICAL, "bad_index", "index not strictly increasing"))
    ohlc = df[["open", "high", "low", "close"]]
    if ohlc.isna().any().any():
        q.issues.append(Issue(CRITICAL, "nan_prices", f"{int(ohlc.isna().any(axis=1).sum())} rows"))
    if (ohlc <= 0).any().any():
        q.issues.append(Issue(CRITICAL, "non_positive_price"))
    bad_hl = ((df["high"] < df["low"]) | (df["high"] < df[["open", "close"]].max(axis=1) - 1e-9)
              | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-9)).sum()
    if bad_hl:
        q.issues.append(Issue(WARNING, "ohlc_inconsistent", f"{int(bad_hl)} rows"))
    if (df["volume"] < 0).any():
        q.issues.append(Issue(CRITICAL, "negative_volume"))
    if (df["volume"].tail(20) == 0).mean() > 0.2:
        q.issues.append(Issue(WARNING, "zero_volume", "many zero-volume days in last 20"))
    if len(df) < min_bars:
        q.issues.append(Issue(CRITICAL, "insufficient_history", f"{len(df)} < {min_bars}"))
    if len(df) > 1:
        rets = df["close"].pct_change().abs()
        big = rets[rets > jump_threshold]
        if len(big):
            q.issues.append(Issue(WARNING, "price_jump", f"{len(big)} daily moves > {jump_threshold:.0%}"))
        # gaps inside history (missing sessions)
        idx = [d.date() for d in df.index]
        have = set(idx)
        expected = cal.trading_days(idx[0], idx[-1])
        missing = [d for d in expected if d not in have]
        if len(missing) > 0:
            run = best = 0
            miss = set(missing)
            for d in expected:
                run = run + 1 if d in miss else 0
                best = max(best, run)
            lvl = CRITICAL if best > max_gap_sessions else WARNING
            q.issues.append(Issue(lvl, "missing_sessions", f"{len(missing)} missing, longest run {best}"))
    q.sessions_behind = cal.sessions_behind(q.last_bar, now, grace_minutes)
    q.stale = q.sessions_behind > max_stale_sessions
    return q


def corwin_schultz_spread(df: pd.DataFrame, window: int = 20) -> float | None:
    """Corwin & Schultz (2012) high-low bid/ask spread proxy, mean over last `window` days.
    Returns a fraction of price (0.002 = 0.2 %) or None if not computable."""
    if len(df) < window + 2:
        return None
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    if (l <= 0).any() or (h < l).any():
        return None
    hl2 = np.log(h / l) ** 2
    beta = hl2[1:] + hl2[:-1]
    hh = np.maximum(h[1:], h[:-1])
    ll = np.minimum(l[1:], l[:-1])
    gamma = np.log(hh / ll) ** 2
    k = 3 - 2 * np.sqrt(2)
    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
    s = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    s = np.clip(s, 0, None)[-window:]
    if not np.isfinite(s).all():
        return None
    return float(s.mean())
