import math
import random

import pytest

from aiscanner.config import RiskCfg
from aiscanner.portfolio.risk import ApprovedOrder, PortfolioState, Proposal, RiskEngine
from aiscanner.portfolio.store import Position


def eng(**kw):
    return RiskEngine(RiskCfg(**kw))


def state(cash=100_000.0, positions=(), prices=None):
    return PortfolioState(cash, list(positions), prices or {})


def codes(d):
    return {r.code for r in d.reasons}


def test_sizing_formula_exact():
    d = eng().evaluate(Proposal("AAA", 50.0, 48.0, last_price=50.0, sector="XLK"), state())
    # equity 100k, 1 % = 1000, risk/share = 2  => 500 shares (= 25k = 25 % of equity -> position cap 20 % violated)
    assert d.risk_budget == 1000 and d.computed_shares == 500
    assert not d.approved and "rk_position_size" in codes(d)
    d = eng().evaluate(Proposal("AAA", 50.0, 48.0, last_price=50.0, sector="XLK"), state(), shares_override=400)
    assert d.approved and d.shares == 400 and d.risk_amount == 800
    assert d.order is not None and d.order.shares == 400


def test_floor_not_round():
    d = eng(max_position_pct=1.0, max_sector_pct=1.0, max_total_risk_pct=0.5, gap_stress_multiple=10).evaluate(
        Proposal("AAA", 100.0, 93.0, last_price=100), state())
    assert d.computed_shares == math.floor(1000 / 7) == 142


def test_invalid_stop_rejected():
    for stop in (50.0, 55.0, 0.0, -1.0, float("nan")):
        d = eng().evaluate(Proposal("AAA", 50.0, stop, last_price=50.0), state())
        assert not d.approved and d.order is None and d.shares == 0


def test_short_not_supported():
    d = eng().evaluate(Proposal("AAA", 50.0, 55.0, side="short", last_price=50.0), state())
    assert codes(d) == {"rk_short_unsupported"}


def test_max_positions():
    pos = [Position(f"S{i}", 1, 10.0, stop=9.0, sector="XLK") for i in range(10)]
    d = eng().evaluate(Proposal("NEW", 50.0, 48.0, last_price=50.0), state(positions=pos, prices={}))
    assert "rk_max_positions" in codes(d) and not d.approved
    d9 = eng().evaluate(Proposal("NEW", 50.0, 48.0, last_price=50.0), state(positions=pos[:9]), shares_override=100)
    assert "rk_max_positions" not in codes(d9)


def test_insufficient_cash():
    d = eng().evaluate(Proposal("AAA", 50.0, 48.0, last_price=50.0), state(cash=1000.0, positions=[Position("X", 1000, 99.0, stop=90.0)],
                                                                        prices={"X": 99.0}), shares_override=100)
    assert "rk_cash" in codes(d)


def test_duplicate_symbol():
    d = eng().evaluate(Proposal("AAA", 50.0, 48.0, last_price=50.0), state(positions=[Position("AAA", 5, 40.0, stop=35.0)]))
    assert "rk_duplicate" in codes(d)


def test_stale_data_blocks():
    d = eng().evaluate(Proposal("AAA", 50.0, 48.0, last_price=50.0, data_ok=False, data_detail="stale"), state(), shares_override=100)
    assert "rk_stale_data" in codes(d) and not d.approved


def test_entry_far_from_last_price():
    d = eng().evaluate(Proposal("AAA", 50.0, 48.0, last_price=40.0), state(), shares_override=100)
    assert "rk_entry_deviation" in codes(d)


def test_sector_limit():
    pos = [Position("A", 600, 50.0, stop=45.0, sector="XLK")]               # 30k of 100k -> equity 100k
    st = state(cash=70_000, positions=pos, prices={"A": 50.0})
    d = eng().evaluate(Proposal("B", 50.0, 47.0, last_price=50.0, sector="XLK"), st, shares_override=200)     # 30k + 10k = 40 % > 35 %
    assert "rk_sector" in codes(d)
    d = eng().evaluate(Proposal("B", 50.0, 47.0, last_price=50.0, sector="XLK"), st, shares_override=100)     # exactly 35 % is allowed
    assert "rk_sector" not in codes(d)
    d = eng().evaluate(Proposal("B", 50.0, 47.0, last_price=50.0, sector="XLF"), st, shares_override=200)
    assert "rk_sector" not in codes(d)


def test_aggregate_open_risk_limit_and_missing_stop_assumption():
    # existing positions with no stop are assumed 8 % at risk
    pos = [Position("A", 100, 100.0, sector="XLK"), Position("B", 100, 100.0, sector="XLF")]   # 20k value -> 1.6k assumed risk
    st = state(cash=80_000, positions=pos, prices={"A": 100.0, "B": 100.0})
    e = eng(max_total_risk_pct=0.017)       # 1.6 % assumed + 0.2 % new = 1.8 % > 1.7 %
    d = e.evaluate(Proposal("C", 50.0, 48.0, last_price=50.0, sector="XLE"), st, shares_override=100)
    assert "rk_total_risk" in codes(d) and any("assumed_stop" in n for n in d.notes)


def test_gap_stress_rejects_very_tight_stops():
    # stop only 0.2 % below entry => huge share count; a 2 % gap through the stop is a ~11x risk-budget loss
    e = eng(max_position_pct=1.0, max_total_risk_pct=1.0, max_sector_pct=1.0)
    d = e.evaluate(Proposal("AAA", 100.0, 99.8, last_price=100.0), state(cash=10_000_000))
    assert "rk_gap_stress" in codes(d) and not d.approved
    assert d.worst_case_loss > e.cfg.gap_stress_multiple * d.risk_budget


def test_suggested_max_shares_passes_when_resubmitted():
    e = eng()
    prop = Proposal("AAA", 50.0, 48.0, last_price=50.0, sector="XLK")
    d = e.evaluate(prop, state())
    assert not d.approved and 0 < d.suggested_max_shares < d.computed_shares
    d2 = e.evaluate(prop, state(), shares_override=d.suggested_max_shares)
    assert d2.approved, [r.code for r in d2.reasons]
    d3 = e.evaluate(prop, state(), shares_override=d.suggested_max_shares + 1)
    assert not d3.approved                     # one share more must break at least one constraint


def test_override_cannot_exceed_formula():
    d = eng().evaluate(Proposal("AAA", 100.0, 95.0, last_price=100.0), state(), shares_override=10_000)
    assert "rk_override_too_big" in codes(d) and not d.approved


def test_zero_shares_when_stop_too_wide():
    d = eng().evaluate(Proposal("AAA", 5000.0, 1000.0, last_price=5000.0), state(cash=100_000))
    assert d.computed_shares == 0 and "rk_zero_shares" in codes(d)


def test_approved_order_cannot_be_forged_or_mutated():
    with pytest.raises(PermissionError):
        ApprovedOrder(object(), "AAA", 10, 50.0, 48.0, None, "", "", 20.0, 1e5)
    d = eng().evaluate(Proposal("AAA", 50.0, 48.0, last_price=50.0), state(), shares_override=100)
    with pytest.raises(AttributeError):
        d.order.shares = 99999


def test_equity_includes_market_value():
    st = state(cash=50_000, positions=[Position("A", 100, 100.0, stop=90.0)], prices={"A": 150.0})
    assert st.equity == 65_000
    d = eng().evaluate(Proposal("B", 50.0, 49.0, last_price=50.0), st)
    assert d.risk_budget == 650.0


def test_metrics():
    pos = [Position("A", 100, 100.0, stop=90.0, sector="XLK"), Position("B", 50, 200.0, stop=180.0, sector="XLF")]
    st = state(cash=80_000, positions=pos, prices={"A": 100.0, "B": 200.0})
    m = eng().metrics(st, peak_equity=120_000)
    assert m["equity"] == 100_000 and m["n_positions"] == 2
    assert m["open_risk"] == 100 * 10 + 50 * 20
    assert m["drawdown"] == pytest.approx(100_000 / 120_000 - 1)
    assert m["sector_weights"]["XLK"] == pytest.approx(0.10)
    assert m["largest_weight"] == pytest.approx(0.10)


def test_property_approved_decisions_always_satisfy_all_limits():
    """Randomised invariant check: whatever the inputs, an approved decision never violates a hard limit."""
    rnd = random.Random(1234)
    e = eng()
    cfg = e.cfg
    approved = 0
    for _ in range(3000):
        n = rnd.randint(0, 11)
        sectors = ["XLK", "XLF", "XLE", "XLV", ""]
        pos = [Position(f"P{i}", rnd.randint(1, 300), rnd.uniform(5, 400), stop=rnd.choice([None, rnd.uniform(3, 350)]),
                        sector=rnd.choice(sectors)) for i in range(n)]
        prices = {p.symbol: p.avg_cost * rnd.uniform(0.7, 1.4) for p in pos}
        st = state(cash=rnd.uniform(0, 300_000), positions=pos, prices=prices)
        entry = rnd.uniform(3, 500)
        stop = entry * rnd.uniform(0.5, 1.05)
        prop = Proposal("NEW", entry, stop, sector=rnd.choice(sectors), last_price=entry * rnd.uniform(0.9, 1.1),
                        data_ok=rnd.random() > 0.1)
        d = e.evaluate(prop, st, shares_override=rnd.choice([None, None, rnd.randint(0, 800)]))
        if not d.approved:
            assert d.order is None
            continue
        approved += 1
        eq = st.equity
        assert prop.stop < prop.entry and prop.data_ok
        assert len(pos) < cfg.max_positions
        assert d.shares <= math.floor(eq * cfg.risk_per_trade / (entry - stop))
        assert d.shares * entry + cfg.fee_per_trade <= st.cash + 1e-6
        assert d.shares * entry / eq <= cfg.max_position_pct + 1e-9
        assert e.total_open_risk(st)[0] + d.risk_amount <= cfg.max_total_risk_pct * eq + 1e-6
        assert d.worst_case_loss <= cfg.gap_stress_multiple * d.risk_budget + 1e-6
    assert approved > 50          # the property test must actually exercise the approval path
