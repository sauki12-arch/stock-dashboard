import numpy as np
import pandas as pd
import pytest

from aiscanner.config import RiskCfg
from aiscanner.data.validation import DataQuality, Issue
from aiscanner.discovery.indicators import build_frame
from aiscanner.models import CRITICAL, WARNING
from aiscanner.portfolio.monitor import PortfolioMonitor, chandelier_stop
from aiscanner.portfolio.risk import PortfolioState, RiskEngine
from aiscanner.portfolio.store import Position
from tests.helpers import mk, trend


def mon(**kw):
    cfg = RiskCfg(**kw)
    return PortfolioMonitor(RiskEngine(cfg), cfg), cfg


def uptrend(n=320, end=140.0):
    c = np.linspace(60, end, n)
    return build_frame(mk(c, hi=c * 1.004, lo=c * 0.996))


def kinds(rep, sev=None):
    return {(a.kind) for a in rep.alerts if sev is None or a.severity == sev}


def test_healthy_position_no_alerts_and_trailing_proposal():
    m, _ = mon()
    fr = uptrend()
    pos = Position("AAA", 100, 100.0, stop=90.0, sector="XLK", strategy="longterm", opened_at="2025-06-01")
    rep = m.evaluate(PortfolioState(50_000, [pos], {"AAA": float(fr.close.iloc[-1])}), {"AAA": fr})
    h = rep.positions[0]
    assert h.status == "ok" and not [a for a in rep.alerts if a.severity == CRITICAL]
    assert h.trailing_stop is not None and 90 < h.trailing_stop < h.price      # proposal only
    assert h.pnl == pytest.approx((h.price - 100.0) * 100)
    assert h.r_multiple == pytest.approx((h.price - 100) / 10)
    assert pos.stop == 90.0                                                      # never auto-applied


def test_stop_breach_is_critical():
    m, _ = mon()
    fr = uptrend()
    price = float(fr.close.iloc[-1])
    pos = Position("AAA", 10, 100.0, stop=price + 1.0, strategy="longterm")
    rep = m.evaluate(PortfolioState(1000, [pos], {}), {"AAA": fr})
    assert "stop_breach" in kinds(rep, CRITICAL) and rep.positions[0].status == "critical"
    a = next(a for a in rep.alerts if a.kind == "stop_breach")
    assert a.key.startswith("stop_breach:AAA:") and "AAA" in a.body


def test_near_stop_is_warning_only():
    m, _ = mon()
    fr = uptrend()
    price, atr = float(fr.close.iloc[-1]), float(fr.atr14.iloc[-1])
    pos = Position("AAA", 10, 100.0, stop=price - 0.3 * atr, strategy="longterm")
    rep = m.evaluate(PortfolioState(1000, [pos], {}), {"AAA": fr})
    assert "near_stop" in kinds(rep, WARNING) and "stop_breach" not in kinds(rep)


def test_swing_trend_break_critical_and_lt_uses_sma200():
    m, _ = mon()
    c = np.r_[np.linspace(60, 130, 280), np.linspace(130, 113, 40)]
    fr = build_frame(mk(c, hi=c * 1.004, lo=c * 0.996))
    row = fr.iloc[-1]
    assert row.close < row.sma50 and row.macd_hist < 0 and row.close > row.sma200
    swing = Position("S", 10, 120.0, strategy="swing")
    lt = Position("L", 10, 120.0, strategy="longterm")
    rep = m.evaluate(PortfolioState(1000, [swing, lt], {}), {"S": fr, "L": fr})
    by = {h.symbol: h for h in rep.positions}
    assert by["S"].status == "critical" and by["L"].status == "warning"      # LT only warns below SMA50


def test_stale_data_flagged():
    m, _ = mon()
    fr = uptrend()
    q = DataQuality("AAA", stale=True, sessions_behind=4, issues=[])
    rep = m.evaluate(PortfolioState(1000, [Position("AAA", 1, 100.0, stop=50.0)], {}), {"AAA": fr}, {"AAA": q})
    assert "stale_data" in kinds(rep) and rep.data_warnings


def test_missing_frame_reports_no_data():
    m, _ = mon()
    rep = m.evaluate(PortfolioState(1000, [Position("ZZZ", 1, 10.0)], {}), {})
    assert rep.positions[0].status == "no_data" and rep.data_warnings


def test_portfolio_level_critical_alerts():
    m, cfg = mon(drawdown_alert_pct=0.10, max_total_risk_pct=0.02, max_sector_pct=0.30)
    fr = uptrend()
    price = float(fr.close.iloc[-1])
    pos = [Position("A", 200, price, stop=price * 0.8, sector="XLK", strategy="longterm"),
           Position("B", 100, price, stop=price * 0.8, sector="XLK", strategy="longterm")]
    st = PortfolioState(10_000, pos, {})
    rep = m.evaluate(st, {"A": fr, "B": fr}, peak_equity=st.equity * 1.25)
    ks = kinds(rep, CRITICAL)
    assert {"portfolio_drawdown", "portfolio_risk", "portfolio_concentration"} <= ks
    assert any(a.kind == "position_size" and a.severity == WARNING for a in rep.alerts)


def test_drawdown_alert_key_changes_per_5pct_bucket():
    m, _ = mon(drawdown_alert_pct=0.10)
    fr = uptrend()
    price = float(fr.close.iloc[-1])
    st = PortfolioState(10_000, [Position("A", 10, price, stop=price * 0.9, strategy="longterm")], {})
    k1 = {a.key for a in m.evaluate(st, {"A": fr}, peak_equity=st.equity / 0.88).alerts if a.kind == "portfolio_drawdown"}
    k2 = {a.key for a in m.evaluate(st, {"A": fr}, peak_equity=st.equity / 0.83).alerts if a.kind == "portfolio_drawdown"}
    assert k1 and k2 and k1 != k2


def test_chandelier_never_above_price():
    fr = uptrend()
    s = chandelier_stop(fr, "")
    assert s < float(fr.close.iloc[-1])
    assert chandelier_stop(fr.head(10), "") is None
