from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiscanner.config import RiskCfg
from aiscanner.data.store import Store
from aiscanner.portfolio.broker import OrderRejected, PaperBroker
from aiscanner.portfolio.importer import load_positions_csv, parse_positions_csv
from aiscanner.portfolio.risk import PortfolioState, Proposal, RiskEngine
from aiscanner.portfolio.store import PortfolioStore, Position

NOW = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)


@pytest.fixture
def env():
    st = Store(":memory:")
    cfg = RiskCfg(slippage_bps=10, fee_per_trade=2.0)
    ps = PortfolioStore(st, 100_000)
    risk = RiskEngine(cfg)
    return ps, risk, PaperBroker(ps, risk, cfg), cfg


def approve(ps, risk, sym="AAA", entry=50.0, stop=47.5, shares=300, sector="XLK"):
    stt = PortfolioState(ps.cash, ps.positions(), {})
    d = risk.evaluate(Proposal(sym, entry, stop, 60.0, sector, "swing", last_price=entry), stt, shares_override=shares)
    assert d.approved, [r.code for r in d.reasons]
    return d.order


def test_open_and_close_cash_accounting(env):
    ps, risk, broker, cfg = env
    order = approve(ps, risk)
    pos = broker.open_position(order, {}, now=NOW, last_price=50.0)
    fill = 50.0 * 1.001
    assert ps.cash == pytest.approx(100_000 - fill * 300 - 2.0)
    assert pos.avg_cost == pytest.approx((fill * 300 + 2.0) / 300)
    assert ps.get("AAA").stop == 47.5
    pl = broker.close_position("AAA", 55.0, now=NOW)
    sell_fill = 55.0 * 0.999
    expected = sell_fill * 300 - 2.0 - pos.avg_cost * 300
    assert pl == pytest.approx(expected)
    assert ps.get("AAA") is None
    assert ps.realized_pl() == pytest.approx(expected)
    assert ps.cash == pytest.approx(100_000 - fill * 300 - 2.0 + sell_fill * 300 - 2.0)
    assert [t.side for t in reversed(ps.trades())] == ["BUY", "SELL"]


def test_partial_close(env):
    ps, risk, broker, _ = env
    broker.open_position(approve(ps, risk), {}, now=NOW, last_price=50.0)
    broker.close_position("AAA", 52.0, qty=100, now=NOW)
    assert ps.get("AAA").qty == 200


def test_broker_requires_approved_order(env):
    ps, risk, broker, _ = env
    with pytest.raises(TypeError):
        broker.open_position({"symbol": "AAA", "shares": 10}, {})


def test_order_revalidated_at_fill_time(env):
    ps, risk, broker, _ = env
    order = approve(ps, risk)
    ps.set_cash(100.0)                                   # cash drained after approval
    with pytest.raises(OrderRejected) as e:
        broker.open_position(order, {}, now=NOW, last_price=50.0)
    assert any(r.code == "rk_cash" for r in e.value.reasons)
    assert ps.get("AAA") is None


def test_stale_data_blocks_fill(env):
    ps, risk, broker, _ = env
    order = approve(ps, risk)
    with pytest.raises(OrderRejected):
        broker.open_position(order, {}, now=NOW, last_price=50.0, data_ok=False, data_detail="stale")


def test_stop_only_raises_unless_forced(env):
    ps, risk, broker, _ = env
    broker.open_position(approve(ps, risk), {}, now=NOW, last_price=50.0)
    broker.update_stop("AAA", 49.0, price=52.0)
    assert ps.get("AAA").stop == 49.0
    with pytest.raises(ValueError):
        broker.update_stop("AAA", 48.0, price=52.0)
    with pytest.raises(ValueError):
        broker.update_stop("AAA", 53.0, price=52.0)
    broker.update_stop("AAA", 48.0, price=52.0, allow_lower=True)
    assert ps.get("AAA").stop == 48.0


def test_manual_position_and_reset(env):
    ps, risk, broker, _ = env
    broker.add_manual_position(Position("MSFT", 10, 400.0, stop=380.0, sector="XLK"))
    assert ps.cash == 100_000 and ps.get("MSFT").qty == 10
    broker.add_manual_position(Position("NVDA", 10, 100.0), affects_cash=True)
    assert ps.cash == 99_000
    with pytest.raises(ValueError):
        broker.add_manual_position(Position("X", 1e6, 100.0), affects_cash=True)
    ps.record_equity("2026-03-09", 101_000, 50_000)
    ps.record_equity("2026-03-10", 99_000, 50_000)
    assert ps.peak_equity() == 101_000
    ps.reset(50_000)
    assert ps.cash == 50_000 and ps.positions() == [] and ps.equity_history() == []


def test_portfolio_survives_reopen(tmp_path):
    p = tmp_path / "p.db"
    ps = PortfolioStore(Store(p), 10_000)
    ps.upsert_position(Position("A", 5, 10.0, stop=9.0, sector="XLK", strategy="swing"))
    ps2 = PortfolioStore(Store(p), 999)                  # starting cash must NOT overwrite persisted cash
    assert ps2.cash == 10_000 and ps2.get("A").strategy == "swing"


# ---------------------------- importer ----------------------------------
def test_example_csv_imports_cleanly():
    path = Path(__file__).resolve().parent.parent / "examples" / "portfolio_example.csv"
    res = load_positions_csv(path)
    assert not res.errors and [p.symbol for p in res.positions] == ["AAPL", "MSFT", "JPM", "XOM"]
    assert res.positions[0].stop == 160.0 and res.positions[2].strategy == "swing"


def test_import_validation_errors():
    txt = ("symbol,qty,cost,stop,sector,strategy\n"
           "aapl,10,100,90,XLK,swing\n"
           "AAPL,10,100,,,\n"                # duplicate
           "BAD SYM,10,100,,,\n"
           "MSFT,-5,100,,,\n"
           "NVDA,abc,100,,,\n"
           "AMD,5,0,,,\n"
           "TSLA,5,100,,NOPE,\n"
           "META,5,100,,,weird\n"
           "GOOG,5,100,,,\n")
    res = parse_positions_csv(txt, sector_lookup={"GOOG": "XLC"})
    assert [p.symbol for p in res.positions] == ["AAPL", "GOOG"]
    assert res.positions[1].sector == "XLC"
    assert len(res.errors) == 7


def test_import_missing_columns_and_empty():
    assert "missing columns" in parse_positions_csv("symbol,qty\nA,1\n").errors[0]
    assert parse_positions_csv("").errors
