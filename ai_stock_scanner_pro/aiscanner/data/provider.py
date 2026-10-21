"""Interchangeable market-data provider interface (swap Yahoo for a paid feed later)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]

# Yahoo-style intraday limits (days of history available per interval). Providers may differ.
INTRADAY_MAX_DAYS = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730, "90m": 60}
INTRADAY_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "90m": 90}


@dataclass(frozen=True)
class ProviderError:
    """Per-symbol failure. Returned (not raised) so one bad ticker never kills a batch."""
    symbol: str
    message: str
    retryable: bool = True

    def __str__(self) -> str:
        return f"{self.symbol}: {self.message}"


class ProviderUnavailable(Exception):
    """Whole-provider failure (network down, HTTP 429, auth)."""


class DataProvider(ABC):
    name: str = "abstract"
    is_synthetic: bool = False
    supports_intraday: bool = False
    disclaimer: str = ""

    @abstractmethod
    def fetch_daily(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame | ProviderError]:
        """Adjusted daily OHLCV for each symbol, index = trading dates (tz-naive, normalized)."""

    def fetch_intraday(self, symbol: str, interval: str, days: int) -> pd.DataFrame | ProviderError:
        return ProviderError(symbol, f"{self.name} does not support intraday data", retryable=False)

    def health_check(self) -> tuple[bool, str]:
        return True, "ok"


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in OHLCV},
                        index=pd.DatetimeIndex([], name="date"))


def normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case columns, keep OHLCV, sort, drop duplicate index, drop all-NaN rows."""
    if df is None or len(df) == 0:
        return empty_frame()
    out = df.rename(columns=str.lower)[OHLCV].astype("float64")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(how="all")
    out.index.name = "date"
    return out
