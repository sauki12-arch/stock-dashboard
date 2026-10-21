from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from aiscanner.data import market_calendar as cal

END = date(2026, 3, 10)


def mk(close, hi=None, lo=None, vol=None, end=END, open_=None):
    """Build an OHLCV frame on real trading dates ending at `end`."""
    c = np.asarray(close, float)
    n = len(c)
    idx = pd.DatetimeIndex(cal.trading_days(end - timedelta(days=int(n * 1.6) + 30), end)[-n:])
    return pd.DataFrame({"open": c if open_ is None else open_,
                         "high": c * 1.005 if hi is None else hi,
                         "low": c * 0.995 if lo is None else lo,
                         "close": c, "volume": np.full(n, 1e6) if vol is None else vol}, index=idx)


def trend(n=400, start=50.0, drift=0.0008, vol=0.01, seed=1, volume=2e6):
    r = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(drift + vol * r.standard_normal(n)))
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) * (1 + 0.004 * r.random(n))
    lo = np.minimum(o, c) * (1 - 0.004 * r.random(n))
    return mk(c, hi=hi, lo=lo, vol=volume * (1 + 0.2 * r.standard_normal(n)).clip(0.3), open_=o)
