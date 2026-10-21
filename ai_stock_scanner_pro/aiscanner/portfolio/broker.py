"""Paper-trading ledger. NO brokerage connectivity exists in this codebase."""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import RiskCfg
from ..models import R, Reason
from .risk import ApprovedOrder, PortfolioState, Proposal, RiskEngine
from .store import PortfolioStore, Position, Trade


class OrderRejected(Exception):
    def __init__(self, reasons: list[Reason]):
        super().__init__("; ".join(r.code for r in reasons))
        self.reasons = reasons


class PaperBroker:
    def __init__(self, pstore: PortfolioStore, risk: RiskEngine, cfg: RiskCfg):
        self.ps, self.risk, self.cfg = pstore, risk, cfg

    def _state(self, prices: dict[str, float]) -> PortfolioState:
        return PortfolioState(self.ps.cash, self.ps.positions(), prices)

    def open_position(self, order: ApprovedOrder, prices: dict[str, float], now: datetime | None = None,
                      last_price: float | None = None, data_ok: bool = True, data_detail: str = "") -> Position:
        """Fill an approved order. The order is re-validated against the CURRENT state (cash, slots, exposure may have
        changed since approval); the risk engine can therefore reject it again."""
        if not isinstance(order, ApprovedOrder):
            raise TypeError("open_position requires an ApprovedOrder from RiskEngine.evaluate()")
        st = self._state(prices)
        again = self.risk.evaluate(Proposal(order.symbol, order.entry, order.stop, order.target, order.sector,
                                            order.strategy, last_price=last_price, data_ok=data_ok,
                                            data_detail=data_detail), st, shares_override=order.shares)
        if not again.approved:
            raise OrderRejected(again.reasons)
        slip = self.cfg.slippage_bps / 1e4
        fill = order.entry * (1 + slip)
        fee = self.cfg.fee_per_trade
        cost = fill * order.shares + fee
        if cost > st.cash + 1e-9:                                   # slippage can push cost above the approved cash check
            raise OrderRejected([R("rk_cash", False, need=cost, have=st.cash)])
        ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        pos = Position(order.symbol, order.shares, cost / order.shares, order.stop, order.target, order.sector,
                       order.strategy, ts, "paper")
        self.ps.set_cash(st.cash - cost)
        self.ps.upsert_position(pos)
        self.ps.add_trade(Trade(ts, order.symbol, "BUY", order.shares, fill, fee, 0.0, "paper"))
        return pos

    def close_position(self, symbol: str, price: float, qty: float | None = None, now: datetime | None = None,
                       note: str = "paper") -> float:
        """Sell (all or part) at `price` minus slippage. Returns realised P/L (after fees)."""
        pos = self.ps.get(symbol)
        if pos is None:
            raise KeyError(symbol)
        q = pos.qty if qty is None else min(float(qty), pos.qty)
        if q <= 0 or price <= 0:
            raise ValueError("qty and price must be > 0")
        fill = price * (1 - self.cfg.slippage_bps / 1e4)
        fee = self.cfg.fee_per_trade
        proceeds = fill * q - fee
        pl = proceeds - pos.avg_cost * q
        self.ps.set_cash(self.ps.cash + proceeds)
        if q >= pos.qty - 1e-9:
            self.ps.delete_position(symbol)
        else:
            pos.qty -= q
            self.ps.upsert_position(pos)
        ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        self.ps.add_trade(Trade(ts, symbol, "SELL", q, fill, fee, pl, note))
        return pl

    def update_stop(self, symbol: str, new_stop: float, price: float | None = None, allow_lower: bool = False) -> None:
        pos = self.ps.get(symbol)
        if pos is None:
            raise KeyError(symbol)
        if new_stop <= 0 or (price is not None and new_stop >= price):
            raise ValueError("stop must be > 0 and below the current price")
        if pos.stop is not None and new_stop < pos.stop and not allow_lower:
            raise ValueError("stops can only be raised (pass allow_lower=True to override manually)")
        pos.stop = new_stop
        self.ps.upsert_position(pos)

    def add_manual_position(self, pos: Position, affects_cash: bool = False) -> None:
        """Record a holding that already exists elsewhere (no risk-engine approval: it is not a new trade)."""
        if affects_cash:
            cost = pos.qty * pos.avg_cost
            if cost > self.ps.cash + 1e-9:
                raise ValueError("insufficient cash")
            self.ps.set_cash(self.ps.cash - cost)
        self.ps.upsert_position(pos)
