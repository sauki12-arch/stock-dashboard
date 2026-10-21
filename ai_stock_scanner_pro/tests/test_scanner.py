import json
import re
import glob
from datetime import datetime, timezone

import pytest

from aiscanner import i18n
from aiscanner.config import Settings
from aiscanner.data.engine import DataEngine
from aiscanner.data.mock import MockProvider
from aiscanner.data.store import Store
from aiscanner.data.universe import Universe, load_universe
from aiscanner.discovery.scanner import Scanner, load_scan

NOW = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def world():
    clock = lambda: NOW                                                # noqa: E731
    u = load_universe()
    st = Store(":memory:")
    p = MockProvider(now=clock, sector_map=u.sector)
    p.fail_symbols = {u.symbols[3]}
    p.missing_symbols = {u.symbols[7]}
    p.stale_symbols = {u.symbols[11]: 5}
    eng = DataEngine(p, st, Settings().data, clock=clock, sleep=lambda s: None)
    rep = eng.update_daily(u.all_fetch_symbols())
    sc = Scanner(eng, st, u, Settings())
    res = sc.run()
    return dict(u=u, st=st, p=p, eng=eng, rep=rep, res=res, sc=sc)


def test_full_500_universe_mock(world):
    res, u = world["res"], world["u"]
    assert res.n_universe == 500
    assert len(res.candidates) + len(res.failed) == 500
    assert res.synthetic is True and "SYNTHETIC" in res.provider
    assert res.n_eligible > 300                          # mock universe is liquid by construction
    assert res.regime["regime"] in ("RISK_ON", "NEUTRAL", "RISK_OFF")


def test_partial_failures_do_not_stop_scan(world):
    res, u = world["res"], world["u"]
    assert u.symbols[3] in res.failed and u.symbols[7] in res.failed
    stale = res.by_symbol(u.symbols[11])
    assert stale is not None and not stale.eligible and "stale" in stale.quality
    assert any(g["code"] == "gate_data_quality" for g in stale.gate_failures)
    assert u.symbols[11] in res.stale_symbols


def test_rankings_sorted_validated_and_explained(world):
    res = world["res"]
    for st in ("swing", "longterm"):
        top = res.ranked(st, 25)
        assert len(top) > 0
        scores = [c.score(st) for c in top]
        assert scores == sorted(scores, reverse=True)
        assert all(c.eligible and c.scores[st]["validated"] for c in top)
        comp = top[0].scores[st]["components"]
        assert sum(v["max"] for v in comp.values()) == pytest.approx(100)
        assert sum(v["points"] for v in comp.values()) == pytest.approx(top[0].score(st), abs=0.1)
        lv = top[0].scores[st]["levels"]
        assert lv["stop"] < lv["entry"] < lv["target"]
    assert len(res.ranked("swing", 10)) <= 10
    assert len(res.ranked("swing", 50)) <= 50


def test_unvalidated_excluded_unless_requested(world):
    res = world["res"]
    all_sw = res.ranked("swing", include_unvalidated=True)
    val_sw = res.ranked("swing")
    assert len(all_sw) >= len(val_sw)


def test_ineligible_have_no_scores_and_reasons(world):
    res = world["res"]
    inel = [c for c in res.candidates if not c.eligible]
    for c in inel:
        assert c.scores == {} and c.gate_failures


def test_persistence_roundtrip(world):
    res, st = world["res"], world["st"]
    back = load_scan(st)
    assert back.run_id == res.run_id and len(back.candidates) == len(res.candidates)
    a, b = res.ranked("swing", 5), back.ranked("swing", 5)
    assert [c.symbol for c in a] == [c.symbol for c in b]
    assert back.regime["regime"] == res.regime["regime"]
    assert back.failed == res.failed
    assert load_scan(st, offset=1) is None


def test_as_of_is_last_completed_session(world):
    assert world["res"].as_of == "2026-03-10"
    assert all(c.last_bar <= "2026-03-10" for c in world["res"].candidates)


def test_cancel_returns_partial_without_persisting():
    clock = lambda: NOW                                                # noqa: E731
    u = Universe(symbols=["AAPL", "MSFT", "NVDA"], sector={})
    st = Store(":memory:")
    p = MockProvider(now=clock)
    eng = DataEngine(p, st, Settings().data, clock=clock, sleep=lambda s: None)
    eng.update_daily(u.all_fetch_symbols())
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1
    res = Scanner(eng, st, u, Settings()).run(cancel=cancel)
    assert len(res.candidates) <= 1 and res.run_id is None and load_scan(st) is None


def test_every_reason_code_in_source_has_hebrew_text():
    src = "".join(open(f, encoding="utf-8").read() for f in glob.glob("aiscanner/**/*.py", recursive=True))
    codes = set(re.findall(r'\bR\("([a-z0-9_]+)"', src)) | set(re.findall(r'need\([^\n]*?, "(val_[a-z0-9_]+)"', src))
    assert codes and not [c for c in codes if c not in i18n.REASONS]


def test_scan_result_json_safe(world):
    c = world["res"].ranked("swing", 1)[0]
    json.dumps(c.scores)
    lines = i18n.render_dicts(c.scores["swing"]["components"]["trend"]["reasons"])
    assert lines and all(isinstance(x, str) and x for x in lines)
