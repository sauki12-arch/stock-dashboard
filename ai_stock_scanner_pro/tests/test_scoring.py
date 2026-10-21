import math

import numpy as np
import pandas as pd
import pytest

from aiscanner.config import ScanCfg
from aiscanner.discovery import scoring as S
from aiscanner.discovery.indicators import build_frame
from aiscanner.discovery.patterns import core_flags
from aiscanner.models import LONGTERM, SWING
from tests.helpers import mk, trend

CFG = ScanCfg()


def prep(df, spy=None, sec=None):
    f = build_frame(df, spy, sec)
    fl = core_flags(f)
    return f.iloc[-1].to_dict(), fl.iloc[-1].to_dict()


def test_helpers():
    assert S.lin(5, 0, 10) == 0.5 and S.lin(-1, 0, 10) == 0 and S.lin(99, 0, 10) == 1 and S.lin(float("nan"), 0, 1) == 0
    assert S.trap(5, 0, 2, 8, 10) == 1 and S.trap(1, 0, 2, 8, 10) == 0.5 and S.trap(11, 0, 2, 8, 10) == 0
    assert S.klass_for(85, CFG) == "high" and S.klass_for(84.9, CFG) == "strong"
    assert S.klass_for(70, CFG) == "strong" and S.klass_for(55, CFG) == "monitor" and S.klass_for(54.9, CFG) == "low"


def test_weights_sum_and_max_points():
    df, spy = trend(420, drift=0.001, seed=21), trend(420, seed=22)
    f, fl = prep(df, spy, spy)
    for st in (SWING, LONGTERM):
        r = S.score_row(st, f, fl, df, CFG)
        assert sum(c.max_points for c in r.components.values()) == pytest.approx(100)
        assert set(r.components) == {"trend", "momentum", "volume", "setup", "relative_strength", "volatility", "risk_reward"}
        for c in r.components.values():
            assert 0 <= c.points <= c.max_points + 1e-9
        assert 0 <= r.total <= 100
        assert r.total == pytest.approx(sum(c.points for c in r.components.values()))
        assert r.version


def test_custom_weights_scale_components():
    df = trend(420, drift=0.001, seed=21)
    f, fl = prep(df)
    base = S.score_row(SWING, f, fl, df, CFG)
    cfg2 = ScanCfg(swing_weights={"trend": 40, "momentum": 10, "volume": 10, "setup": 10, "relative_strength": 10,
                                  "volatility": 10, "risk_reward": 10})
    r2 = S.score_row(SWING, f, fl, df, cfg2)
    ratio = base.components["trend"].points / 20
    assert r2.components["trend"].points == pytest.approx(ratio * 40)


def test_strong_uptrend_scores_above_downtrend():
    spy = trend(420, drift=0.0003, seed=40)
    up = trend(420, drift=0.0015, vol=0.008, seed=41)
    dn = trend(420, drift=-0.0015, vol=0.008, seed=42)
    for st in (SWING, LONGTERM):
        su = S.score_row(st, *prep(up, spy, spy), up, CFG)
        sd = S.score_row(st, *prep(dn, spy, spy), dn, CFG)
        assert su.total > sd.total + 15
    lt_dn = S.score_row(LONGTERM, *prep(dn, spy, spy), dn, CFG)
    assert not lt_dn.validated
    codes = {r.code for r in lt_dn.failures}
    assert "val_below_sma200" in codes


def test_levels_are_sane():
    df = trend(420, drift=0.001, seed=21)
    f, fl = prep(df)
    for st in (SWING, LONGTERM):
        lv = S.compute_levels(st, f, df)
        assert lv.stop < lv.entry < lv.target and lv.atr > 0 and lv.rr > 0
        assert lv.entry - lv.stop >= 1.0 * lv.atr - 1e-9
    sw = S.compute_levels(SWING, f, df)
    assert sw.entry - sw.stop <= 3.0 * sw.atr + 1e-9


def test_levels_none_when_atr_missing():
    assert S.compute_levels(SWING, {"close": 10.0, "atr14": float("nan")}, None) is None
    assert S.compute_levels(SWING, {"close": 10.0, "atr14": 0.0}, None) is None


def test_eligibility_gates():
    df = trend(420, start=50, seed=21, volume=2e6)
    f, _ = prep(df)
    ok = S.check_eligibility(f, len(df), True, [], 0.002, CFG)
    assert ok == []
    codes = lambda r: {x.code for x in r}                       # noqa: E731
    assert "gate_data_quality" in codes(S.check_eligibility(f, len(df), False, ["stale"], 0.002, CFG))
    assert "gate_history" in codes(S.check_eligibility(f, 100, True, [], 0.002, CFG))
    assert "gate_spread" in codes(S.check_eligibility(f, len(df), True, [], 0.05, CFG))
    assert S.check_eligibility(f, len(df), True, [], None, CFG) == []         # spread unavailable => not a gate
    cheap = dict(f, close=2.0)
    assert "gate_price" in codes(S.check_eligibility(cheap, len(df), True, [], 0.0, CFG))
    illiquid = dict(f, dollar_vol20=1e6)
    assert "gate_liquidity" in codes(S.check_eligibility(illiquid, len(df), True, [], 0.0, CFG))
    noatr = dict(f, atr14=float("nan"))
    assert "gate_risk_uncomputable" in codes(S.check_eligibility(noatr, len(df), True, [], 0.0, CFG))


def test_swing_validation_rules():
    df = trend(420, drift=0.0012, seed=21)
    f, fl = prep(df)
    f_bad = dict(f, rsi14=88.0)
    r = S.score_row(SWING, f_bad, fl, df, CFG)
    assert "val_overbought" in {x.code for x in r.failures} and not r.validated
    fl_none = {k: False for k in fl}
    r = S.score_row(SWING, f, fl_none, df, CFG)
    assert "val_no_setup" in {x.code for x in r.failures}
    r = S.score_row(SWING, f, dict(fl_none, breakout=True), df, CFG)
    f_lowvol = dict(f, relvol=0.8)
    r = S.score_row(SWING, f_lowvol, dict(fl_none, breakout=True), df, CFG)
    assert "val_breakout_volume" in {x.code for x in r.failures}


def test_experimental_patterns_listed_but_not_scored():
    df = trend(420, drift=0.001, seed=21)
    f, fl = prep(df)
    a = S.score_row(SWING, f, fl, df, CFG, experimental=[])
    b = S.score_row(SWING, f, fl, df, CFG, experimental=["cup_and_handle"])
    assert a.total == b.total and b.experimental == ["cup_and_handle"]


def test_missing_data_earns_no_points():
    f = {"close": 100.0, "atr14": 2.0}
    r = S.score_row(SWING, f, {}, None, CFG)
    assert r.total < 20
    assert not r.validated


def test_to_dict_is_json_safe():
    import json
    df = trend(420, drift=0.001, seed=21)
    r = S.score_row(SWING, *prep(df), df, CFG)
    json.dumps(r.to_dict())


def test_score_is_causal():
    df, spy = trend(420, drift=0.001, seed=21), trend(420, seed=22)
    full_f = build_frame(df, spy, spy)
    full_fl = core_flags(full_f)
    cut = 360
    a = S.score_row(SWING, full_f.iloc[cut].to_dict(), full_fl.iloc[cut].to_dict(), df.iloc[:cut + 1], CFG)
    sub = build_frame(df.iloc[:cut + 1], spy.iloc[:cut + 1], spy.iloc[:cut + 1])
    b = S.score_row(SWING, sub.iloc[-1].to_dict(), core_flags(sub).iloc[-1].to_dict(), df.iloc[:cut + 1], CFG)
    assert a.total == pytest.approx(b.total)
