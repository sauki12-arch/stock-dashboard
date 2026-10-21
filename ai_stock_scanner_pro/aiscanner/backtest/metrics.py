"""Performance statistics with explicit statistical-meaningfulness guards."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MIN_TRADES = 30
MIN_DAYS_SHARPE = 120


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1).min())


@dataclass
class Stats:
    n_trades: int = 0
    win_rate: float | None = None
    win_rate_ci: tuple[float, float] | None = None
    avg_win_pct: float | None = None
    avg_loss_pct: float | None = None
    expectancy_r: float | None = None
    expectancy_pct: float | None = None
    profit_factor: float | None = None
    total_return: float = 0.0
    cagr: float | None = None
    max_drawdown: float = 0.0
    sharpe: float | None = None
    avg_exposure: float | None = None
    avg_bars_held: float | None = None
    meaningful: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def compute_stats(trades: pd.DataFrame, equity: pd.Series, exposure: pd.Series | None = None) -> Stats:
    s = Stats(n_trades=len(trades))
    if len(equity) >= 2:
        s.total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
        s.max_drawdown = max_drawdown(equity)
        years = (equity.index[-1] - equity.index[0]).days / 365.25
        if years >= 1.0 and equity.iloc[0] > 0 and equity.iloc[-1] > 0:
            s.cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
        elif years < 1.0:
            s.notes.append("period < 1 year: CAGR not reported")
        rets = equity.pct_change().dropna()
        if len(rets) >= MIN_DAYS_SHARPE and rets.std() > 0 and len(trades) >= MIN_TRADES:
            s.sharpe = float(rets.mean() / rets.std() * math.sqrt(252))
        else:
            s.notes.append("Sharpe not reported: need >=%d days and >=%d trades" % (MIN_DAYS_SHARPE, MIN_TRADES))
    if exposure is not None and len(exposure):
        s.avg_exposure = float(exposure.mean())
    if len(trades):
        pnl, ret = trades["pnl"], trades["ret_pct"]
        wins = int((pnl > 0).sum())
        s.avg_bars_held = float(trades["bars"].mean())
        s.win_rate = wins / len(trades)
        s.win_rate_ci = wilson_interval(wins, len(trades))
        s.avg_win_pct = float(ret[pnl > 0].mean()) if wins else None
        s.avg_loss_pct = float(ret[pnl <= 0].mean()) if wins < len(trades) else None
        s.expectancy_r = float(trades["r"].mean())
        s.expectancy_pct = float(ret.mean())
        gross_win, gross_loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
        s.profit_factor = float(gross_win / gross_loss) if gross_loss > 0 else None
    s.meaningful = len(trades) >= MIN_TRADES
    if not s.meaningful:
        s.notes.append(f"only {len(trades)} trades (<{MIN_TRADES}): win rate / expectancy are NOT statistically meaningful")
    return s


def buy_hold(close: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    c = close[(close.index >= start) & (close.index <= end)]
    if len(c) < 2:
        return {"total_return": None, "max_drawdown": None}
    return {"total_return": float(c.iloc[-1] / c.iloc[0] - 1), "max_drawdown": max_drawdown(c / c.iloc[0])}


DISCLOSURES = [
    "SURVIVORSHIP BIAS: the universe is today's list of liquid stocks. Companies that were delisted, acquired or collapsed "
    "are absent, which overstates historical performance.",
    "Data: adjusted Yahoo/vendor prices are revised over time; results depend on the adjustment state at download time.",
    "Execution model: signals at the close of day t are filled at the NEXT day's open with slippage and commissions; stops "
    "fill at the stop (or at the open if the bar gaps through it); if stop and target are both touched in one bar the stop is assumed first.",
    "No liquidity/market-impact model beyond fixed slippage; no borrowing, taxes, dividends timing or intraday path knowledge.",
    "Past performance, even out-of-sample, does not guarantee future results. This is decision support, not a forecast.",
]
