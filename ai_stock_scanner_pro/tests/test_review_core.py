import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from aiscanner import secrets
from aiscanner.config import Settings
from aiscanner.core import AppCore
from aiscanner.data.store import Store, dumps
from aiscanner.portfolio.store import Position
from aiscanner.review import weekly as W
from tests.helpers import mk

NOW = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------- review
def seed_store(n_runs=40, win_ratio=0.5, comp_gap=True):
    """Fabricated scan history + forward bars so outcomes are fully controlled."""
    st = Store(":memory:")
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2026-01-02", periods=80)
    spy = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": np.linspace(100, 104, 80), "volume": 1e6}, index=idx)
    st.upsert_bars("SPY", spy)
    k = 0
    for i in range(n_runs):
        as_of = idx[10 + i % 20].strftime("%Y-%m-%d")
        with st.conn() as c:
            cur = c.execute("INSERT INTO scan_runs(ts,as_of,regime,n_universe,n_eligible,provider,synthetic,version,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                            (as_of, as_of, "RISK_ON", 1, 1, "t", 0, "v", "{}"))
            rid = cur.lastrowid
        win = rng.random() < win_ratio
        sym = f"S{k}"
        k += 1
        entry = 100.0
        fut = [{"open": 100.0, "high": 110.0 if win else 100.5, "low": 99.5 if win else 90.0, "close": 105.0 if win else 92.0, "volume": 1e6}] * 12
        before = [{"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1e6}]
        dates = list(pd.bdate_range(end=pd.Timestamp(as_of), periods=1)) + list(pd.bdate_range(pd.Timestamp(as_of) + pd.Timedelta(days=1), periods=12))
        st.upsert_bars(sym, pd.DataFrame(before + fut, index=pd.DatetimeIndex(dates)))
        good = 0.9 if (win and comp_gap) else 0.4
        comps = {key: {"points": good * 10, "max": 10} for key in ("trend", "momentum", "volume", "setup", "relative_strength", "volatility", "risk_reward")}
        payload = {"symbol": sym, "score": {"levels": {"entry": entry, "stop": 95.0, "target": 105.0}, "components": comps}}
        with st.conn() as c:
            c.execute("INSERT INTO scan_results VALUES(?,?,?,?,?,?,?,?)", (rid, sym, "swing", 80.0, "strong", 1, 1, dumps(payload)))
    return st


def test_forward_resolution_logic():
    idx = pd.bdate_range("2026-01-02", periods=6)
    bars = pd.DataFrame({"open": 100.0, "high": [100, 101, 106, 101, 101, 101], "low": [100, 99, 99, 99, 99, 99],
                         "close": 100.0, "volume": 1}, index=idx)
    assert W._forward(bars, "2026-01-02", 5, 100.0, 95.0, 105.0)[0] == "target"
    bars2 = bars.copy(); bars2.iloc[2, bars2.columns.get_loc("low")] = 94.0
    assert W._forward(bars2, "2026-01-02", 5, 100.0, 95.0, 105.0)[0] == "stop"          # stop checked first on same bar
    assert W._forward(bars, "2026-02-01", 5, 100.0, 95.0, 105.0)[0] == "no_data"


def test_report_summarises_and_flags_false_positives():
    st = seed_store(40, win_ratio=0.5)
    rep = W.build_report(st, NOW, lookback_weeks=26)
    s = rep.summary["swing"]
    assert s["n"] == 40 and s["n_resolved"] == 40
    assert 0.2 < s["target_rate"] < 0.8 and s["target_rate"] + s["stop_rate"] == pytest.approx(1.0)
    assert len(rep.false_positives) == round(s["stop_rate"] * 40)
    md = rep.to_markdown()
    assert "סקירה שבועית" in md and "לא משנה אוטומטית" in md


def test_suggestions_require_sample_and_are_proposals_only():
    small = W.build_report(seed_store(10), NOW, lookback_weeks=26)
    assert small.suggestions == [] and any("מדגם קטן" in n for n in small.notes)
    big = W.build_report(seed_store(60, comp_gap=True), NOW, lookback_weeks=26)
    assert big.suggestions and all(s["direction"] in "+-" for s in big.suggestions)
    flat = W.build_report(seed_store(60, comp_gap=False), NOW, lookback_weeks=26)
    assert flat.suggestions == []


def test_regime_errors_detected():
    st = seed_store(5)
    with st.conn() as c:
        c.execute("UPDATE scan_runs SET regime='RISK_OFF'")
    st.upsert_bars("SPY", pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": np.linspace(100, 130, 80), "volume": 1}, index=pd.bdate_range("2026-01-02", periods=80)))
    rep = W.build_report(st, NOW, lookback_weeks=26)
    assert rep.regime_errors and rep.regime_errors[0]["regime"] == "RISK_OFF"


def test_score_versions_approval_gate():
    sv = W.ScoreVersions(Store(":memory:"))
    w = {"trend": 22, "momentum": 15, "volume": 13, "setup": 15, "relative_strength": 10, "volatility": 10, "risk_reward": 15}
    with pytest.raises(ValueError):
        sv.propose("swing", {**w, "trend": 50})
    v = sv.propose("swing", w, "more trend")
    assert sv.approved_weights("swing") is None                       # a proposal is never active
    with pytest.raises(PermissionError):
        sv.approve(v, "me")                                           # no backtest attached
    sv.attach_backtest(v, {"oos_expectancy_r": 0.1})
    with pytest.raises(PermissionError):
        sv.approve(v, "  ")                                           # reviewer name required
    sv.approve(v, "Reviewer")
    assert sv.approved_weights("swing") == w and sv.list()[0]["approved_by"] == "Reviewer"
    with pytest.raises(KeyError):
        sv.approve("nope", "x")


def test_save_report(tmp_path):
    rep = W.build_report(seed_store(5), NOW, lookback_weeks=26)
    p = W.save_report(rep, tmp_path / "reports")
    assert p.endswith(".md") and "סקירה" in open(p, encoding="utf-8").read()


# ---------------------------------------------------------------- core / jobs
@pytest.fixture
def core(tmp_path):
    from aiscanner.data.universe import Universe, load_universe
    full = load_universe()
    syms = ["AAPL", "MSFT"] + [x for x in full.symbols if x not in ("AAPL", "MSFT")][:38]
    small = Universe(symbols=syms, sector={x: full.sector[x] for x in syms})      # 40 symbols keeps the suite fast
    return AppCore(Settings(), clock=lambda: NOW, db_path=tmp_path / "t.db", sleep=lambda x: None, universe=small)


def test_core_end_to_end_job_with_portfolio_and_alerts(core):
    core.update_data()
    last = core.store.load_bars("AAPL").close.iloc[-1]
    core.broker.add_manual_position(Position("AAPL", 10, last, stop=last * 1.5, sector="XLK", strategy="longterm"))   # stop above price => breach
    r = core.run_job("post_close")
    assert r.ok, r.messages
    assert r.scan.n_universe == 40 and r.monitor.positions[0].status == "critical"
    kinds = {o.alert.kind: o.status for o in r.outcomes}
    assert kinds["stop_breach"] == "not_sent_no_telegram"             # no credentials in the test environment
    assert any(a["kind"] == "stop_breach" for a in core.alerts.history())
    assert core.store.get_job_run("post_close")
    assert core.pstore.equity_history()                               # snapshot recorded
    # synthetic data never produces pushable new-setup alerts
    ns = [a for a in core.alerts.history() if a["kind"] == "new_setup"]
    assert ns and all(a["severity"] == "info" for a in ns)


def test_core_critical_alert_sent_through_telegram(core, monkeypatch):
    secrets.save_telegram("123:ABC", "42")
    sent = []
    from aiscanner.alerts import telegram as T

    class Fake:
        def __init__(self, *a, **k): pass
        def send(self, text):
            sent.append(text)
            return T.SendResult(True, 200)
    monkeypatch.setattr("aiscanner.core.TelegramClient", Fake)
    core.refresh_telegram()
    core.update_data()
    last = core.store.load_bars("AAPL").close.iloc[-1]
    core.broker.add_manual_position(Position("AAPL", 10, last, stop=last * 1.5, sector="XLK"))
    core.settings.alerts.quiet_start = core.settings.alerts.quiet_end = "00:00"
    core.alerts.cfg.quiet_start = core.alerts.cfg.quiet_end = "00:00"
    core.monitor()
    out = core.alerts.process(core.monitor().alerts)
    assert any(o.status == "sent" for o in out) and sent and "AAPL" in sent[0]


def test_core_job_failure_is_reported_not_raised(core):
    core.provider.flaky_batches = 10_000
    r = core.run_job("pre_market")
    assert r.ok                                                      # provider down is a handled condition, scan runs on empty store
    assert r.update.provider_down
    assert core.run_job("bogus").ok is False


def test_core_evaluate_proposal_uses_data_quality(core):
    core.update_data()
    last = float(core.store.load_bars("MSFT").close.iloc[-1])
    d = core.evaluate_proposal("MSFT", last, last * 0.95, shares_override=10)
    assert d.approved and d.order.shares == 10
    core.provider.stale_symbols["MSFT"] = 0
    d2 = core.evaluate_proposal("NOSUCH", 50.0, 48.0)
    assert not d2.approved and any(r.code == "rk_stale_data" for r in d2.reasons)


def test_core_intraday_job_uses_live_price(core):
    core.update_data()
    last = float(core.store.load_bars("AAPL").close.iloc[-1])
    core.broker.add_manual_position(Position("AAPL", 5, last, stop=last * 0.5, sector="XLK"))
    core.clock = core.engine.clock = core.provider._now = lambda: datetime(2026, 3, 10, 16, 0, tzinfo=timezone.utc)
    r = core.run_job("intraday")
    assert r.ok and "intraday prices for 1/1" in r.messages[0]


def test_core_due_jobs_respects_last_run(core):
    core.settings.schedule.enabled = True
    assert core.due_jobs() == ["post_close", "weekly_review"] or "post_close" in core.due_jobs()
    core.store.set_job_run("post_close", NOW.isoformat())
    assert "post_close" not in core.due_jobs()


def test_weekly_review_job_writes_report_with_portfolio_section(core, tmp_path):
    core.update_data()
    last = float(core.store.load_bars("AAPL").close.iloc[-1])
    core.broker.add_manual_position(Position("AAPL", 10, last, stop=last * 0.8, sector="XLK", strategy="longterm"))
    r = core.run_job("weekly_review")
    assert r.ok, r.messages
    path = [m for m in r.messages if m.startswith("weekly report:")][0].split(": ", 1)[1]
    text = open(path, encoding="utf-8").read()
    assert "סקירה שבועית" in text and "סקירת תיק שבועית" in text and "AAPL" in text
