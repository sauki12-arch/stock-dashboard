"""Portfolio persistence on top of the shared SQLite store (positions, cash, trades, equity history)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..data.store import Store, utcnow_iso


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float
    stop: float | None = None
    target: float | None = None
    sector: str = ""
    strategy: str = ""          # "swing" | "longterm" | ""
    opened_at: str = ""
    notes: str = ""

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_cost


@dataclass
class Trade:
    ts: str
    symbol: str
    side: str
    qty: float
    price: float
    fees: float
    realized_pl: float
    note: str = ""


class PortfolioStore:
    def __init__(self, store: Store, starting_cash: float = 100_000.0):
        self.store = store
        if store.kv_get("cash") is None:
            store.kv_set("cash", starting_cash)
            store.kv_set("starting_equity", starting_cash)

    # ---- cash --------------------------------------------------------
    @property
    def cash(self) -> float:
        return float(self.store.kv_get("cash", "0"))

    def set_cash(self, value: float) -> None:
        if value < 0:
            raise ValueError("cash cannot be negative")
        self.store.kv_set("cash", round(float(value), 6))

    @property
    def starting_equity(self) -> float:
        return float(self.store.kv_get("starting_equity", "0"))

    # ---- positions ---------------------------------------------------
    def positions(self) -> list[Position]:
        with self.store.conn() as c:
            rows = c.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [Position(r["symbol"], r["qty"], r["avg_cost"], r["stop"], r["target"], r["sector"] or "",
                         r["strategy"] or "", r["opened_at"] or "", r["notes"] or "") for r in rows]

    def get(self, symbol: str) -> Position | None:
        return next((p for p in self.positions() if p.symbol == symbol), None)

    def upsert_position(self, p: Position) -> None:
        if p.qty <= 0 or p.avg_cost <= 0:
            raise ValueError("qty and avg_cost must be > 0")
        with self.store.conn() as c:
            c.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?,?,?,?)",
                      (p.symbol, p.qty, p.avg_cost, p.stop, p.target, p.sector, p.strategy,
                       p.opened_at or utcnow_iso(), p.notes))

    def delete_position(self, symbol: str) -> None:
        with self.store.conn() as c:
            c.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    def clear_positions(self) -> None:
        with self.store.conn() as c:
            c.execute("DELETE FROM positions")

    # ---- trades ------------------------------------------------------
    def add_trade(self, t: Trade) -> None:
        with self.store.conn() as c:
            c.execute("INSERT INTO trades(ts,symbol,side,qty,price,fees,realized_pl,note) VALUES(?,?,?,?,?,?,?,?)",
                      (t.ts, t.symbol, t.side, t.qty, t.price, t.fees, t.realized_pl, t.note))

    def trades(self, limit: int = 500) -> list[Trade]:
        with self.store.conn() as c:
            rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Trade(r["ts"], r["symbol"], r["side"], r["qty"], r["price"], r["fees"], r["realized_pl"], r["note"] or "")
                for r in rows]

    def realized_pl(self) -> float:
        with self.store.conn() as c:
            return float(c.execute("SELECT COALESCE(SUM(realized_pl),0) s FROM trades WHERE side='SELL'").fetchone()["s"])

    # ---- equity history ---------------------------------------------
    def record_equity(self, date: str, equity: float, cash: float) -> None:
        with self.store.conn() as c:
            c.execute("INSERT OR REPLACE INTO equity_history VALUES(?,?,?)", (date, equity, cash))

    def equity_history(self) -> list[tuple[str, float, float]]:
        with self.store.conn() as c:
            return [(r["date"], r["equity"], r["cash"]) for r in
                    c.execute("SELECT * FROM equity_history ORDER BY date")]

    def peak_equity(self) -> float:
        with self.store.conn() as c:
            r = c.execute("SELECT MAX(equity) m FROM equity_history").fetchone()
        return float(r["m"]) if r and r["m"] is not None else 0.0

    def reset(self, starting_cash: float) -> None:
        with self.store.conn() as c:
            for t in ("positions", "trades", "equity_history"):
                c.execute(f"DELETE FROM {t}")
        self.store.kv_set("cash", starting_cash)
        self.store.kv_set("starting_equity", starting_cash)
