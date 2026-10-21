"""Layer 4 – deterministic risk engine. Pure functions + dataclasses; no AI, no I/O, no randomness.

    risk_budget = equity × risk_per_trade
    shares      = floor(risk_budget / |entry − stop|)          (long only)

Every constraint is evaluated and ALL violations are reported. The only way to obtain an
`ApprovedOrder` (the sole input accepted by `PaperBroker.open_position`) is `RiskEngine.evaluate()` returning
approved=True – the constructor is guarded so callers cannot mint one by hand.
Stop-losses are not guaranteed fills: the gap stress (gap_through_pct) and costs are included in
`worst_case_loss`, which is itself a constraint.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..config import RiskCfg
from ..models import R, Reason
from .store import Position

_MINT_KEY = object()


class ApprovedOrder:
    """Immutable proof that RiskEngine approved these exact numbers."""
    __slots__ = ("symbol", "shares", "entry", "stop", "target", "sector", "strategy", "risk_amount", "equity_at_approval")

    def __init__(self, _key, symbol, shares, entry, stop, target, sector, strategy, risk_amount, equity):
        if _key is not _MINT_KEY:
            raise PermissionError("ApprovedOrder can only be created by RiskEngine.evaluate()")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "shares", int(shares))
        object.__setattr__(self, "entry", float(entry))
        object.__setattr__(self, "stop", float(stop))
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "sector", sector)
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "risk_amount", float(risk_amount))
        object.__setattr__(self, "equity_at_approval", float(equity))

    def __setattr__(self, k, v):
        raise AttributeError("ApprovedOrder is immutable")


@dataclass
class PortfolioState:
    cash: float
    positions: list[Position]
    prices: dict[str, float]                 # last completed close per symbol (may be missing)

    def price_of(self, p: Position) -> float:
        return self.prices.get(p.symbol) or p.avg_cost

    @property
    def market_value(self) -> float:
        return sum(p.qty * self.price_of(p) for p in self.positions)

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    def sector_values(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions:
            out[p.sector or "?"] = out.get(p.sector or "?", 0.0) + p.qty * self.price_of(p)
        return out


@dataclass
class Proposal:
    symbol: str
    entry: float
    stop: float
    target: float | None = None
    sector: str = ""
    strategy: str = ""
    side: str = "long"
    last_price: float | None = None          # latest completed close, for sanity check
    data_ok: bool = True
    data_detail: str = ""


@dataclass
class Decision:
    approved: bool
    shares: int
    reasons: list[Reason]
    risk_budget: float = 0.0
    risk_amount: float = 0.0                 # shares × |entry − stop|
    position_value: float = 0.0
    position_pct: float = 0.0
    worst_case_loss: float = 0.0
    computed_shares: int = 0                 # per the sizing formula (before any human override)
    suggested_max_shares: int = 0            # largest size that would pass every size/cash constraint
    notes: list[str] = field(default_factory=list)
    order: ApprovedOrder | None = None


def open_risk_of(p: Position, price: float, assumed_pct: float) -> tuple[float, bool]:
    """Money at risk from `price` to the stop. Returns (risk, assumed)."""
    if p.stop is not None and p.stop > 0:
        return max(0.0, price - p.stop) * p.qty, False
    return price * assumed_pct * p.qty, True


class RiskEngine:
    def __init__(self, cfg: RiskCfg):
        self.cfg = cfg

    def total_open_risk(self, st: PortfolioState) -> tuple[float, int]:
        tot, n_assumed = 0.0, 0
        for p in st.positions:
            r, a = open_risk_of(p, st.price_of(p), self.cfg.assumed_risk_pct_no_stop)
            tot += r
            n_assumed += int(a)
        return tot, n_assumed

    def _per_share_worst(self, entry: float, stop: float) -> float:
        c = self.cfg
        return (entry - stop) + c.gap_through_pct * entry + 2 * c.slippage_bps / 1e4 * entry

    def evaluate(self, prop: Proposal, st: PortfolioState, shares_override: int | None = None) -> Decision:
        c = self.cfg
        reasons: list[Reason] = []
        notes: list[str] = []
        equity = st.equity
        budget = equity * c.risk_per_trade
        d = Decision(False, 0, reasons, risk_budget=budget, notes=notes)

        if prop.side != "long":
            reasons.append(R("rk_short_unsupported", False))
            return d
        vals = (prop.entry, prop.stop)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in vals):
            reasons.append(R("rk_invalid_input", False, what="entry/stop"))
            return d
        if equity <= 0:
            reasons.append(R("rk_invalid_input", False, what="equity<=0"))
            return d
        if prop.stop >= prop.entry:
            reasons.append(R("rk_invalid_stop", False, entry=prop.entry, stop=prop.stop))
            return d

        if not prop.data_ok:
            reasons.append(R("rk_stale_data", False, detail=prop.data_detail or "n/a"))
        if prop.last_price is not None and prop.last_price > 0:
            dev = abs(prop.entry - prop.last_price) / prop.last_price
            if dev > c.max_entry_deviation_pct:
                reasons.append(R("rk_entry_deviation", False, pct=dev, last=prop.last_price))
        else:
            notes.append("no_last_price")
        if any(p.symbol == prop.symbol for p in st.positions):
            reasons.append(R("rk_duplicate", False, symbol=prop.symbol))
        if len(st.positions) >= c.max_positions:
            reasons.append(R("rk_max_positions", False, n=c.max_positions))

        dist = prop.entry - prop.stop
        calc = int(math.floor(budget / dist))
        d.computed_shares = calc
        shares = calc
        if shares_override is not None:
            if shares_override > calc:
                reasons.append(R("rk_override_too_big", False, calc=calc))
            shares = max(0, int(shares_override))
        d.shares = shares
        if shares < 1:
            reasons.append(R("rk_zero_shares", False))
            return d

        value = shares * prop.entry
        fee = c.fee_per_trade
        d.position_value = value
        d.position_pct = value / equity
        d.risk_amount = shares * dist
        d.worst_case_loss = shares * self._per_share_worst(prop.entry, prop.stop) + 2 * fee

        # --- size / exposure constraints (also used to derive suggested_max_shares) ---
        caps = [calc]
        need_cash = value + fee
        if need_cash > st.cash + 1e-9:
            reasons.append(R("rk_cash", False, need=need_cash, have=st.cash))
        caps.append(int(math.floor(max(0.0, st.cash - fee) / prop.entry)))

        if d.position_pct > c.max_position_pct + 1e-12:
            reasons.append(R("rk_position_size", False, pct=d.position_pct, max=c.max_position_pct))
        caps.append(int(math.floor(c.max_position_pct * equity / prop.entry)))

        existing_risk, n_assumed = self.total_open_risk(st)
        if n_assumed:
            notes.append(f"assumed_stop_for_{n_assumed}_positions")
        cap_risk = c.max_total_risk_pct * equity
        if existing_risk + d.risk_amount > cap_risk + 1e-9:
            reasons.append(R("rk_total_risk", False, pct=(existing_risk + d.risk_amount) / equity, max=c.max_total_risk_pct))
        caps.append(int(math.floor(max(0.0, cap_risk - existing_risk) / dist)))

        if prop.sector:
            sec_val = st.sector_values().get(prop.sector, 0.0)
            if (sec_val + value) / equity > c.max_sector_pct + 1e-12:
                reasons.append(R("rk_sector", False, sector=prop.sector, pct=(sec_val + value) / equity, max=c.max_sector_pct))
            caps.append(int(math.floor(max(0.0, c.max_sector_pct * equity - sec_val) / prop.entry)))
        else:
            notes.append("sector_unknown")

        if d.worst_case_loss > c.gap_stress_multiple * budget + 1e-9:
            reasons.append(R("rk_gap_stress", False, loss=d.worst_case_loss, mult=c.gap_stress_multiple))
        caps.append(int(math.floor((c.gap_stress_multiple * budget - 2 * fee) / self._per_share_worst(prop.entry, prop.stop))))

        d.suggested_max_shares = max(0, min(caps))
        d.approved = not reasons
        if d.approved:
            d.order = ApprovedOrder(_MINT_KEY, prop.symbol, shares, prop.entry, prop.stop, prop.target, prop.sector,
                                    prop.strategy, d.risk_amount, equity)
        return d

    # ---- portfolio-level metrics -------------------------------------
    def metrics(self, st: PortfolioState, peak_equity: float = 0.0, returns=None) -> dict:
        eq = st.equity
        mv = st.market_value
        weights = {p.symbol: p.qty * st.price_of(p) / eq for p in st.positions} if eq > 0 else {}
        sectors = {k: v / eq for k, v in st.sector_values().items()} if eq > 0 else {}
        open_risk, n_assumed = self.total_open_risk(st)
        w = np.array(list(weights.values())) if weights else np.array([])
        peak = max(peak_equity, eq)
        corr = float("nan")
        if returns is not None and returns.shape[1] >= 2:
            cm = returns.dropna(axis=1, thresh=20).corr().to_numpy()
            if cm.shape[0] >= 2:
                corr = float(np.nanmean(cm[np.triu_indices_from(cm, k=1)]))
        return {
            "equity": eq, "cash": st.cash, "market_value": mv, "n_positions": len(st.positions),
            "exposure_pct": mv / eq if eq > 0 else 0.0, "weights": weights, "sector_weights": sectors,
            "largest_weight": float(w.max()) if len(w) else 0.0, "top3_weight": float(np.sort(w)[-3:].sum()) if len(w) else 0.0,
            "hhi": float((w ** 2).sum()) if len(w) else 0.0,
            "open_risk": open_risk, "open_risk_pct": open_risk / eq if eq > 0 else 0.0, "assumed_stop_positions": n_assumed,
            "peak_equity": peak, "drawdown": eq / peak - 1 if peak > 0 else 0.0, "avg_correlation": corr,
        }
