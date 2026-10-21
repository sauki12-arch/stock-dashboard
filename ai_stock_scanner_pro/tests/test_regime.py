import numpy as np
import pandas as pd
import pytest

from aiscanner.brain import regime as B
from aiscanner.discovery.indicators import build_frame
from aiscanner.models import NEUTRAL, RISK_OFF, RISK_ON
from tests.helpers import mk, trend


def frames(spy_kw, qqq_kw=None):
    spy = build_frame(trend(400, **spy_kw))
    qqq = build_frame(trend(400, **(qqq_kw or spy_kw)))
    return spy, qqq


def breadth(p50, p200, nh=0.1, corr=0.35, n=300):
    return B.Breadth(n=n, pct_above_sma50=p50, pct_above_sma200=p200, new_high_minus_low=nh, avg_correlation=corr)


def test_risk_on_in_healthy_market():
    spy, qqq = frames(dict(drift=0.0009, vol=0.006, seed=1, start=300))
    vix = pd.DataFrame({"close": np.full(len(spy), 13.5)}, index=spy.index)
    a = B.assess(spy, qqq, vix, {}, breadth(0.75, 0.7, 0.2, 0.3))
    assert a.regime == RISK_ON and a.score > 62
    assert a.posture == "full" and a.calibration == "uncalibrated"
    assert 0 < a.confidence <= 1
    assert a.evidence and a.invalidations
    assert a.missing == []


def test_risk_off_in_bear_market():
    spy, qqq = frames(dict(drift=-0.002, vol=0.014, seed=2, start=400))
    vix = pd.DataFrame({"close": np.full(len(spy), 35.0)}, index=spy.index)
    a = B.assess(spy, qqq, vix, {}, breadth(0.15, 0.1, -0.2, 0.8))
    assert a.regime == RISK_OFF and a.score < 40
    assert a.suggested_max_positions < 10 and a.posture == "defensive"


def test_missing_inputs_lower_coverage_and_are_reported():
    spy, qqq = frames(dict(drift=0.0009, vol=0.006, seed=1, start=300))
    a = B.assess(spy, qqq, None, {}, None)
    assert set(a.missing) >= {"breadth", "correlation"}
    full = B.assess(spy, qqq, pd.DataFrame({"close": np.full(len(spy), 13.5)}, index=spy.index), {}, breadth(0.75, 0.7))
    assert a.confidence < full.confidence


def test_insufficient_data_defaults_to_neutral_with_zero_confidence():
    a = B.assess(None, None, None, {}, None)
    assert a.regime == NEUTRAL and a.confidence == 0.0 and a.override == "insufficient_data"
    short = build_frame(trend(100))
    a = B.assess(short, short, None, {}, None)
    assert a.regime == NEUTRAL and a.confidence == 0.0


def test_risk_on_blocked_below_200dma():
    # long uptrend then sharp drop below the 200-day average, but other inputs still glowing
    rise = np.linspace(100, 200, 300)
    fall = np.linspace(200, 120, 100)
    c = np.r_[rise, fall]
    spy = build_frame(mk(c, hi=c * 1.003, lo=c * 0.997))
    assert spy["close"].iloc[-1] < spy["sma200"].iloc[-1]
    a = B.assess(spy, spy, pd.DataFrame({"close": np.full(len(spy), 12.0)}, index=spy.index), {}, breadth(0.9, 0.9, 0.3, 0.2))
    assert a.regime != RISK_ON
    assert a.regime == RISK_OFF and a.override == "deep_drawdown_below_200"


def test_sector_ranking_orders_by_relative_strength():
    spy = trend(400, drift=0.0003, vol=0.004, seed=3)
    strong = build_frame(trend(400, drift=0.0015, vol=0.004, seed=4), spy)
    weak = build_frame(trend(400, drift=-0.0010, vol=0.004, seed=5), spy)
    mid = build_frame(trend(400, drift=0.0004, vol=0.004, seed=6), spy)
    spy_f = build_frame(spy)
    a = B.assess(spy_f, spy_f, None, {"XLK": strong, "XLE": weak, "XLF": mid}, breadth(0.6, 0.6))
    assert a.strongest_sectors[0][0] == "XLK" and a.weakest_sectors[0][0] == "XLE"


def test_breadth_computation():
    snaps = [{"close": 10, "sma50": 9, "sma200": 8, "high50_prev": 10.5, "low50_prev": 7},
             {"close": 10, "sma50": 11, "sma200": 12, "high50_prev": 14, "low50_prev": 10},
             {"close": 10, "sma50": 9, "sma200": 12, "high50_prev": 9, "low50_prev": 7},
             {"close": 10, "sma50": 9, "sma200": 8, "high50_prev": 20, "low50_prev": 5}]
    rng = np.random.default_rng(0)
    base = rng.standard_normal(80)
    rets = pd.DataFrame({f"S{i}": 0.8 * base + 0.6 * rng.standard_normal(80) for i in range(8)})
    b = B.compute_breadth(snaps, rets)
    assert b.n == 4 and b.pct_above_sma50 == pytest.approx(0.75) and b.pct_above_sma200 == pytest.approx(0.5)
    assert b.new_high_minus_low == pytest.approx(0.25 - 0.25)
    assert 0.3 < b.avg_correlation < 0.9


def test_to_dict_json_safe():
    import json
    spy, qqq = frames(dict(drift=0.0009, vol=0.006, seed=1, start=300))
    json.dumps(B.assess(spy, qqq, None, {}, breadth(0.6, 0.6)).to_dict())
