"""Walk-forward validation: choose the score threshold on a training window, evaluate ONLY on the following unseen
test window, roll forward, and report the stitched out-of-sample result."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .engine import BTConfig, BTResult, simulate
from .metrics import DISCLOSURES, Stats, buy_hold, compute_stats


@dataclass
class WFWindow:
    train: tuple[str, str]
    test: tuple[str, str]
    chosen_threshold: float
    train_expectancy_r: float | None
    train_trades: int
    oos: BTResult
    fallback_used: bool = False


@dataclass
class WFResult:
    windows: list[WFWindow]
    oos_trades: pd.DataFrame
    oos_equity: pd.Series
    stats: Stats
    benchmark: dict
    disclosures: list[str] = field(default_factory=lambda: list(DISCLOSURES))


def walk_forward(signals: pd.DataFrame, frames: dict[str, pd.DataFrame], spy: pd.DataFrame, cfg: BTConfig,
                 sector_map: dict[str, str] | None = None, train_days: int = 504, test_days: int = 126,
                 thresholds: tuple[float, ...] = (60.0, 70.0, 80.0), min_train_trades: int = 10,
                 default_threshold: float = 70.0) -> WFResult:
    dates = spy.index
    wins: list[WFWindow] = []
    i = 0
    while i + train_days + test_days <= len(dates):
        tr0, tr1 = dates[i], dates[i + train_days - 1]
        te0, te1 = dates[i + train_days], dates[i + train_days + test_days - 1]
        best_th, best_e, best_n, fb = default_threshold, None, 0, True
        for th in thresholds:
            r = simulate(signals, frames, spy, _with(cfg, th), sector_map, tr0, tr1)
            if r.stats.n_trades >= min_train_trades and r.stats.expectancy_r is not None:
                if best_e is None or r.stats.expectancy_r > best_e:
                    best_th, best_e, best_n, fb = th, r.stats.expectancy_r, r.stats.n_trades, False
        oos = simulate(signals, frames, spy, _with(cfg, best_th), sector_map, te0, te1)
        wins.append(WFWindow((str(tr0.date()), str(tr1.date())), (str(te0.date()), str(te1.date())), best_th, best_e,
                             best_n, oos, fb))
        i += test_days
    if not wins:
        raise ValueError("not enough history for even one walk-forward window")
    trades = pd.concat([w.oos.trades for w in wins], ignore_index=True)
    # chain each window's equity curve (flat start each window) into one stitched OOS curve
    parts, level = [], cfg.initial_equity
    for w in wins:
        e = w.oos.equity / w.oos.equity.iloc[0] * level
        parts.append(e)
        level = float(e.iloc[-1])
    eq = pd.concat(parts)
    stats = compute_stats(trades, eq)
    stats.notes.append("positions are closed at each window end (no carry-over); thresholds chosen on training data only")
    return WFResult(wins, trades, eq, stats, buy_hold(spy["close"], eq.index[0], eq.index[-1]))


def _with(cfg: BTConfig, threshold: float) -> BTConfig:
    import dataclasses as dc
    return dc.replace(cfg, threshold=threshold)
