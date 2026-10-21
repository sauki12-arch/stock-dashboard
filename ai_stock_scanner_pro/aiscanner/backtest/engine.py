"""Event-driven backtester for the scanner's own scoring rules.

Timeline of one simulated day d (no look-ahead):
  1. fill orders created at the close of d-1 at OPEN(d) (+slippage) – only if RiskEngine (the live rules) approves
  2. process exits on bar d (gap-aware stops; stop wins if stop & target both touched)
  3. mark equity at CLOSE(d)
  4. generate new signals from data ≤ d (decision at the close) -> executed on d+1
Signals are precomputed ONCE per symbol with causal indicators (see tests: truncating future data leaves signals unchanged).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..config import RiskCfg, ScanCfg
from ..discovery.indicators import build_frame
from ..discovery.patterns import core_flags
from ..discovery.scoring import score_row
from ..models import LONGTERM, SWING
from ..portfolio.risk import PortfolioState, Proposal, RiskEngine
from ..portfolio.store import Position
from .metrics import DISCLOSURES, Stats, buy_hold, compute_stats

SIZE_CODES = {"rk_position_size", "rk_cash", "rk_total_risk", "rk_sector", "rk_gap_stress"}
SWING_SETUPS = ("breakout", "near_breakout", "pullback_support", "sma20_reclaim", "tight_consolidation", "volatility_contraction")


@dataclass
class BTConfig:
    strategy: str = SWING
    threshold: float = 70.0
    initial_equity: float = 100_000.0
    max_positions: int = 10
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.20
    max_total_risk_pct: float = 0.08
    max_sector_pct: float = 0.35
    slippage_bps: float = 5.0
    commission: float = 1.0
    max_hold_bars: int | None = None            # default: swing 10, long-term 126
    trail_atr_mult: float | None = None         # default: swing None, long-term 4.0
    eval_step: int | None = None                # default: swing 1, long-term 5 (bars between evaluations)
    warmup: int = 220
    max_new_per_day: int = 3
    min_record_score: float = 55.0
    resize_to_limits: bool = True               # if only size/cash/exposure limits fail, take the largest permitted size

    def resolved(self) -> "BTConfig":
        import dataclasses as dc
        c = dc.replace(self)
        if c.max_hold_bars is None:
            c.max_hold_bars = 10 if c.strategy == SWING else 126
        if c.trail_atr_mult is None and c.strategy == LONGTERM:
            c.trail_atr_mult = 4.0
        if c.eval_step is None:
            c.eval_step = 1 if c.strategy == SWING else 5
        return c

    def risk_cfg(self) -> RiskCfg:
        return RiskCfg(starting_cash=self.initial_equity, risk_per_trade=self.risk_per_trade, max_positions=self.max_positions,
                       max_position_pct=self.max_position_pct, max_sector_pct=self.max_sector_pct,
                       max_total_risk_pct=self.max_total_risk_pct, slippage_bps=self.slippage_bps,
                       fee_per_trade=self.commission)


@dataclass
class BTResult:
    config: BTConfig
    trades: pd.DataFrame
    equity: pd.Series
    exposure: pd.Series
    stats: Stats
    benchmark: dict
    rejected: dict[str, int]
    disclosures: list[str] = field(default_factory=lambda: list(DISCLOSURES))
    period: tuple[date, date] | None = None


# -------------------------------------------------------------------- signals
def build_signals(frames: dict[str, pd.DataFrame], spy: pd.DataFrame | None, sector_dfs: dict[str, pd.DataFrame],
                  sector_map: dict[str, str], scan_cfg: ScanCfg, cfg: BTConfig, progress=None) -> pd.DataFrame:
    """One row per (symbol, decision date) that is eligible, validated and scores ≥ min_record_score."""
    cfg = cfg.resolved()
    rows = []
    syms = list(frames)
    for k, sym in enumerate(syms):
        if progress:
            progress(k, len(syms), sym)
        df = frames[sym]
        if len(df) < cfg.warmup + 5:
            continue
        f = build_frame(df, spy, sector_dfs.get(sector_map.get(sym, "")))
        fl = core_flags(f)
        c = f["close"]
        gate = (c >= scan_cfg.min_price) & (f["dollar_vol20"] >= scan_cfg.min_dollar_volume) & (f["atr14"] > 0)
        if cfg.strategy == SWING:
            pre = (gate & (c > f["sma50"]) & (f["rsi14"] < 80) & f["atr_pct"].between(0.01, 0.08)
                   & fl[list(SWING_SETUPS)].any(axis=1))
        else:
            pre = (gate & (c > f["sma200"]) & (f["sma50"] > f["sma200"]) & (f["sma200_chg20"] >= 0) & (f["rs126_spy"] > 0)
                   & (f["atr_pct"] <= 0.06) & (f["dist_high252"] > -0.35)
                   & (f["dollar_vol20"] >= scan_cfg.longterm_min_dollar_volume))
        idx = np.flatnonzero(pre.to_numpy())
        idx = idx[(idx >= cfg.warmup) & (idx % cfg.eval_step == 0)]
        for i in idx:
            row = f.iloc[i].to_dict()
            hist = df.iloc[max(0, i - 130): i + 1]                       # data ≤ decision date only
            res = score_row(cfg.strategy, row, fl.iloc[i].to_dict(), hist, scan_cfg)
            if not res.validated or res.total < cfg.min_record_score or res.levels is None:
                continue
            lv = res.levels
            rows.append((sym, df.index[i], res.total, lv.stop, lv.target, lv.rr, lv.atr, float(c.iloc[i])))
    out = pd.DataFrame(rows, columns=["symbol", "date", "score", "stop", "target", "rr", "atr", "close"])
    return out.sort_values(["date", "score"], ascending=[True, False]).reset_index(drop=True)


# -------------------------------------------------------------------- simulation
@dataclass
class _Pos:
    shares: int
    entry_px: float
    entry_date: pd.Timestamp
    entry_i: int
    stop: float
    init_stop: float
    target: float
    highest_close: float
    sector: str
    score: float


def simulate(signals: pd.DataFrame, frames: dict[str, pd.DataFrame], spy: pd.DataFrame, cfg: BTConfig,
             sector_map: dict[str, str] | None = None, start: pd.Timestamp | None = None,
             end: pd.Timestamp | None = None) -> BTResult:
    cfg = cfg.resolved()
    sector_map = sector_map or {}
    dates = spy.index
    if start is not None:
        dates = dates[dates >= start]
    if end is not None:
        dates = dates[dates <= end]
    if len(dates) < 2:
        raise ValueError("period too short")
    slip = cfg.slippage_bps / 1e4
    engine = RiskEngine(cfg.risk_cfg())
    atr_cache: dict[str, pd.Series] = {}
    arr: dict[str, dict] = {}
    for s, df in frames.items():
        arr[s] = {"idx": {d: i for i, d in enumerate(df.index)}, "o": df["open"].to_numpy(), "h": df["high"].to_numpy(),
                  "l": df["low"].to_numpy(), "c": df["close"].to_numpy()}
    if cfg.trail_atr_mult:
        from ..discovery.indicators import atr as _atr
        for s, df in frames.items():
            atr_cache[s] = _atr(df).to_numpy()
    sig_by_date: dict[pd.Timestamp, list] = {}
    sg = signals[(signals["score"] >= cfg.threshold)]
    for d, grp in sg.groupby("date"):
        sig_by_date[d] = list(grp.itertuples(index=False))

    cash = cfg.initial_equity
    pos: dict[str, _Pos] = {}
    pending: list = []
    trades: list[dict] = []
    eq, expo = [], []
    rejected: Counter = Counter()
    last_close: dict[str, float] = {}

    def close_pos(sym: str, d: pd.Timestamp, i: int, px: float, reason: str):
        nonlocal cash
        p = pos.pop(sym)
        proceeds = px * p.shares - cfg.commission
        cash += proceeds
        pnl = proceeds - (p.entry_px * p.shares + cfg.commission)
        risk = p.shares * (p.entry_px - p.init_stop)
        trades.append({"symbol": sym, "entry_date": p.entry_date, "exit_date": d, "entry_px": p.entry_px, "exit_px": px,
                       "shares": p.shares, "pnl": pnl, "ret_pct": px / p.entry_px - 1 - 2 * cfg.commission / (p.entry_px * p.shares),
                       "r": pnl / risk if risk > 0 else 0.0, "bars": i - p.entry_i, "reason": reason, "score": p.score})

    for di, d in enumerate(dates):
        # 1) fills from yesterday's signals at today's open
        for sg_ in pending:
            a = arr.get(sg_.symbol)
            if a is None or d not in a["idx"] or sg_.symbol in pos:
                continue
            i = a["idx"][d]
            entry = float(a["o"][i]) * (1 + slip)
            if entry <= sg_.stop or entry >= sg_.target:
                rejected["gap_invalidated"] += 1
                continue
            plist = [Position(s, p.shares, p.entry_px, p.stop, sector=p.sector) for s, p in pos.items()]
            state = PortfolioState(cash, plist, {s: last_close.get(s, p.entry_px) for s, p in pos.items()})
            prop = Proposal(sg_.symbol, entry, sg_.stop, sg_.target, sector_map.get(sg_.symbol, ""), cfg.strategy,
                            last_price=float(a["c"][i - 1]) if i > 0 else None)
            dec = engine.evaluate(prop, state)
            if (not dec.approved and cfg.resize_to_limits and dec.suggested_max_shares >= 1
                    and {r.code for r in dec.reasons} <= SIZE_CODES):
                dec = engine.evaluate(prop, state, shares_override=dec.suggested_max_shares)
                rejected["resized_to_limits"] += 1
            if not dec.approved:
                for r in dec.reasons:
                    rejected[r.code] += 1
                continue
            cash -= entry * dec.shares + cfg.commission
            pos[sg_.symbol] = _Pos(dec.shares, entry, d, di, sg_.stop, sg_.stop, sg_.target, entry,
                                   sector_map.get(sg_.symbol, ""), float(sg_.score))
        pending = []

        # 2) exits on today's bar
        for sym in list(pos):
            a = arr[sym]
            if d not in a["idx"]:
                continue
            i = a["idx"][d]
            p = pos[sym]
            o, h, l, c = a["o"][i], a["h"][i], a["l"][i], a["c"][i]
            if o <= p.stop:
                close_pos(sym, d, di, float(o) * (1 - slip), "stop_gap")
            elif l <= p.stop:
                close_pos(sym, d, di, p.stop * (1 - slip), "stop")
            elif h >= p.target:
                close_pos(sym, d, di, float(max(o, p.target)), "target")
            elif di - p.entry_i >= cfg.max_hold_bars:
                close_pos(sym, d, di, float(c) * (1 - slip), "time")
            elif cfg.trail_atr_mult:
                p.highest_close = max(p.highest_close, float(c))
                at = atr_cache[sym][i]
                if at == at:                                         # not NaN
                    p.stop = max(p.stop, p.highest_close - cfg.trail_atr_mult * at)

        # 3) mark to market
        for sym, a in arr.items():
            if d in a["idx"]:
                last_close[sym] = float(a["c"][a["idx"][d]])
        mv = sum(p.shares * last_close.get(s, p.entry_px) for s, p in pos.items())
        eq.append(cash + mv)
        expo.append(mv / (cash + mv) if cash + mv > 0 else 0.0)

        # 4) new signals from data ≤ d
        if di < len(dates) - 1 and d in sig_by_date:
            slots = cfg.max_positions - len(pos)
            for s_ in sig_by_date[d]:
                if slots <= 0 or len(pending) >= cfg.max_new_per_day:
                    break
                if s_.symbol in pos or any(x.symbol == s_.symbol for x in pending):
                    continue
                pending.append(s_)
                slots -= 1

    last_d = dates[-1]
    for sym in list(pos):                                           # force-close so period stats are self-contained
        a = arr[sym]
        close_pos(sym, last_d, len(dates) - 1, last_close.get(sym, pos[sym].entry_px) * (1 - slip), "end_of_period")
    if eq:
        eq[-1] = cash
    equity = pd.Series(eq, index=dates, name="equity")
    exposure = pd.Series(expo, index=dates)
    tdf = pd.DataFrame(trades, columns=["symbol", "entry_date", "exit_date", "entry_px", "exit_px", "shares", "pnl",
                                        "ret_pct", "r", "bars", "reason", "score"])
    stats = compute_stats(tdf, equity, exposure)
    bench = buy_hold(spy["close"], dates[0], dates[-1])
    return BTResult(cfg, tdf, equity, exposure, stats, bench, dict(rejected), period=(dates[0].date(), dates[-1].date()))
