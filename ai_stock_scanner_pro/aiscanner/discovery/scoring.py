"""Transparent 0-100 scoring engine (Swing and Long-Term scored separately).

Points: trend 20, momentum 15, volume 15, setup 15, relative strength 10, volatility 10, risk/reward 15.
Weights are configurable (must sum to 100) and each component is a weighted *average* of
sub-scores in [0,1], never a sum, which limits double-counting of correlated signals. Signal families
are kept disjoint on purpose:
  trend = moving-average structure/slope/weekly alignment (no oscillators, no returns)
  momentum = RSI / MACD / acceleration (swing) or multi-month returns/persistence (long-term)
  volume = relative volume, OBV, up/down-volume, liquidity
  relative strength = excess returns vs SPY/sector only
  setup = price-geometry patterns (volume confirmation is scored in `volume`, not here)
Experimental patterns are listed but contribute 0 points.
"""
from __future__ import annotations

import math
from typing import Mapping

import numpy as np
import pandas as pd

from .. import SCORING_VERSION
from ..config import ScanCfg
from ..models import (KLASS_HIGH, KLASS_LOW, KLASS_MONITOR, KLASS_STRONG, LONGTERM, R, SWING,
                      ComponentScore, Reason, ScoreResult, TradeLevels)
from .patterns import CORE, nearest_levels


# ---------------------------------------------------------------- helpers
def lin(x: float, lo: float, hi: float) -> float:
    """0 at/below lo, 1 at/above hi; NaN -> 0 (missing evidence never earns points)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return float(min(1.0, max(0.0, (x - lo) / (hi - lo))))


def trap(x: float, a: float, b: float, c: float, d: float) -> float:
    """Trapezoid: 0 below a, ramps to 1 at b, 1 until c, ramps to 0 at d."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if x <= a or x >= d:
        return 0.0
    if x < b:
        return (x - a) / (b - a)
    if x <= c:
        return 1.0
    return (d - x) / (d - c)


def _g(f: Mapping, k: str, default: float = float("nan")) -> float:
    v = f.get(k, default)
    return default if v is None else float(v)


def _gt(a: float, b: float) -> bool:
    return not (math.isnan(a) or math.isnan(b)) and a > b


def klass_for(total: float, cfg: ScanCfg) -> str:
    if total >= cfg.band_high:
        return KLASS_HIGH
    if total >= cfg.band_strong:
        return KLASS_STRONG
    if total >= cfg.band_monitor:
        return KLASS_MONITOR
    return KLASS_LOW


# ---------------------------------------------------------------- levels
def compute_levels(strategy: str, f: Mapping, hist: pd.DataFrame | None) -> TradeLevels | None:
    """Stop/target from ATR and pivot structure. Returns None if not computable."""
    close, atr = _g(f, "close"), _g(f, "atr14")
    if not (close > 0 and atr > 0):
        return None
    support = resistance = None
    if hist is not None and len(hist) >= 30:
        support, resistance = nearest_levels(hist, close)
    if strategy == SWING:
        if support is not None:
            stop, basis = support - 0.25 * atr, "support"
            if stop > close - 1.0 * atr:
                stop, basis = close - 1.0 * atr, "min_1atr"
        else:
            stop, basis = close - 2.0 * atr, "2atr"
        if stop < close - 3.0 * atr:
            stop, basis = close - 3.0 * atr, "max_3atr"
        if resistance is not None and resistance - close > 0.1 * atr:
            target, tb = resistance, "resistance"
        else:
            target, tb = close + 3.0 * atr, "atr_projection"
    else:
        sma200 = _g(f, "sma200")
        stop, basis = close - 4.0 * atr, "4atr"
        if not math.isnan(sma200) and sma200 < close and sma200 - 0.5 * atr > stop:
            stop, basis = sma200 - 0.5 * atr, "sma200"
        if stop > close - 1.5 * atr:
            stop, basis = close - 1.5 * atr, "min_1.5atr"
        r252 = _g(f, "ret252", 0.0)
        r252 = 0.0 if math.isnan(r252) else r252
        target = close * (1 + min(0.30, max(0.08, 0.3 * max(r252, 0.0))))
        tb = "trend_projection"
    if stop <= 0 or stop >= close:
        return None
    return TradeLevels(entry=close, stop=stop, target=target, atr=atr, support=support, resistance=resistance,
                       stop_basis=basis, target_basis=tb)


# ---------------------------------------------------------------- eligibility
def check_eligibility(f: Mapping, n_bars: int, quality_ok: bool, quality_codes: list[str],
                      spread: float | None, cfg: ScanCfg) -> list[Reason]:
    """Hard gates applied before ranking. Empty list = eligible."""
    out: list[Reason] = []
    if not quality_ok:
        out.append(R("gate_data_quality", False, codes=",".join(quality_codes)))
    if n_bars < cfg.min_bars:
        out.append(R("gate_history", False, bars=n_bars, need=cfg.min_bars))
    close = _g(f, "close")
    if not (close >= cfg.min_price):
        out.append(R("gate_price", False, price=close, need=cfg.min_price))
    dv = _g(f, "dollar_vol20")
    if not (dv >= cfg.min_dollar_volume):
        out.append(R("gate_liquidity", False, dv=dv, need=cfg.min_dollar_volume))
    if spread is not None and spread > cfg.max_spread_proxy:
        out.append(R("gate_spread", False, spread=spread, need=cfg.max_spread_proxy))
    atr = _g(f, "atr14")
    if not (atr > 0):
        out.append(R("gate_risk_uncomputable", False))
    return out


# ---------------------------------------------------------------- components: swing
def _swing(f: Mapping, flags: Mapping, lv: TradeLevels | None, w: Mapping) -> dict[str, ComponentScore]:
    c = _g(f, "close")
    comps: dict[str, ComponentScore] = {}

    # TREND
    struct = np.mean([float(_gt(c, _g(f, "ema20"))), float(_gt(_g(f, "ema20"), _g(f, "ema50"))),
                      float(_gt(c, _g(f, "sma50"))),
                      float(_gt(_g(f, "sma50"), _g(f, "sma200"))) if not math.isnan(_g(f, "sma200")) else 0.5])
    slope = 0.5 * lin(_g(f, "sma20_chg5"), -0.01, 0.03) + 0.5 * lin(_g(f, "sma50_chg10"), -0.01, 0.04)
    wk = _g(f, "wk_align", 0.0)
    wk = 0.0 if math.isnan(wk) else wk
    s = 0.4 * struct + 0.3 * slope + 0.3 * wk
    rs = [R("trend_struct", struct >= 0.5, v=struct), R("trend_slope", slope >= 0.4, v=slope),
          R("trend_weekly", wk >= 1, aligned=bool(wk))]
    comps["trend"] = ComponentScore("trend", w["trend"] * s, w["trend"], rs)

    # MOMENTUM
    rsi_v = _g(f, "rsi14")
    rsi_s = trap(rsi_v, 40, 52, 66, 80)
    hist_pos = float(_gt(_g(f, "macd_hist"), 0))
    hist_up = float(_gt(_g(f, "macd_hist_chg3"), 0))
    macd_s = 0.5 * hist_pos + 0.5 * hist_up
    acc_s = lin(_g(f, "mom_accel"), -0.02, 0.03)
    s = 0.45 * rsi_s + 0.35 * macd_s + 0.20 * acc_s
    rs = [R("mom_rsi", 40 <= rsi_v <= 70 if not math.isnan(rsi_v) else None, v=rsi_v),
          R("mom_macd", macd_s >= 0.5, pos=bool(hist_pos), rising=bool(hist_up)),
          R("mom_accel", acc_s >= 0.5, v=_g(f, "mom_accel"))]
    comps["momentum"] = ComponentScore("momentum", w["momentum"] * s, w["momentum"], rs)

    # VOLUME
    rv = _g(f, "relvol")
    rv_s = trap(rv, 0.6, 1.2, 2.5, 5.0)
    acc = 0.5 * lin(_g(f, "obv_trend20"), -0.2, 0.4) + 0.5 * lin(_g(f, "updown20"), 0.8, 1.6)
    dv = _g(f, "dollar_vol20")
    liq = lin(math.log10(dv) if dv and dv > 0 else float("nan"), 7.3, 8.7)
    s = 0.45 * rv_s + 0.40 * acc + 0.15 * liq
    rs = [R("vol_rel", rv >= 1.2 if not math.isnan(rv) else None, v=rv),
          R("vol_accum", acc >= 0.5, v=acc), R("vol_liq", liq >= 0.5, dv=dv)]
    comps["volume"] = ComponentScore("volume", w["volume"] * s, w["volume"], rs)

    # SETUP
    ranks = {"breakout": 1.0, "pullback_support": 0.9, "sma20_reclaim": 0.75, "near_breakout": 0.7,
             "tight_consolidation": 0.6, "volatility_contraction": 0.5}
    present = [k for k in ranks if flags.get(k)]
    base = max((ranks[k] for k in present), default=0.0)
    bonus = 0.1 if (len(present) >= 2 and base > 0) else 0.0
    s = min(1.0, base + bonus)
    rs = [R("setup_present", True, name=k) for k in present] or [R("setup_none", False)]
    comps["setup"] = ComponentScore("setup", w["setup"] * s, w["setup"], rs)

    # RELATIVE STRENGTH
    sp63, sp20, sec63, line = _g(f, "rs63_spy"), _g(f, "rs20_spy"), _g(f, "rs63_sec"), _g(f, "rsline_spy")
    s = (0.4 * lin(sp63, -0.05, 0.20) + 0.2 * lin(sp20, -0.03, 0.10)
         + 0.2 * lin(sec63, -0.05, 0.15) + 0.2 * lin(line, -0.02, 0.10))
    rs = [R("rs_spy", sp63 > 0 if not math.isnan(sp63) else None, v=sp63),
          R("rs_sector", sec63 > 0 if not math.isnan(sec63) else None, v=sec63),
          R("rs_line", line > 0 if not math.isnan(line) else None, v=line)]
    comps["relative_strength"] = ComponentScore("relative_strength", w["relative_strength"] * s, w["relative_strength"], rs)

    # VOLATILITY
    ap, rv20 = _g(f, "atr_pct"), _g(f, "rv20")
    s = 0.6 * trap(ap, 0.008, 0.018, 0.045, 0.09) + 0.4 * trap(rv20, 0.15, 0.25, 0.55, 0.90)
    rs = [R("volat_atr", 0.01 <= ap <= 0.06 if not math.isnan(ap) else None, v=ap),
          R("volat_rv", None, v=rv20)]
    comps["volatility"] = ComponentScore("volatility", w["volatility"] * s, w["volatility"], rs)

    comps["risk_reward"] = _rr_component(lv, w, stop_best=(1.0, 2.5))
    return comps


def _rr_component(lv: TradeLevels | None, w: Mapping, stop_best: tuple[float, float]) -> ComponentScore:
    if lv is None:
        return ComponentScore("risk_reward", 0.0, w["risk_reward"], [R("rr_none", False)])
    rr = lv.rr
    stop_atr = (lv.entry - lv.stop) / lv.atr
    s = 0.8 * lin(rr, 1.0, 3.0) + 0.2 * trap(stop_atr, stop_best[0] - 0.5, stop_best[0], stop_best[1], stop_best[1] + 1.0)
    rs = [R("rr_ratio", rr >= 1.5, rr=rr, stop=lv.stop, target=lv.target, basis=lv.target_basis),
          R("rr_stop_atr", None, v=stop_atr, basis=lv.stop_basis)]
    return ComponentScore("risk_reward", w["risk_reward"] * s, w["risk_reward"], rs)


# ---------------------------------------------------------------- components: long-term
def _longterm(f: Mapping, flags: Mapping, lv: TradeLevels | None, w: Mapping) -> dict[str, ComponentScore]:
    c = _g(f, "close")
    comps: dict[str, ComponentScore] = {}

    struct = np.mean([float(_gt(c, _g(f, "sma200"))), float(_gt(_g(f, "sma50"), _g(f, "sma200"))),
                      float(_gt(c, _g(f, "sma50")))])
    slope = lin(_g(f, "sma200_chg20"), -0.01, 0.05)
    wk = _g(f, "wk_align", 0.0)
    wk = 0.0 if math.isnan(wk) else wk
    dist = lin(_g(f, "dist_high252"), -0.30, -0.02)
    s = 0.35 * struct + 0.20 * slope + 0.25 * wk + 0.20 * dist
    rs = [R("trend_struct", struct >= 0.67, v=struct), R("trend_slope200", slope >= 0.3, v=_g(f, "sma200_chg20")),
          R("trend_weekly", wk >= 1, aligned=bool(wk)), R("trend_dist_high", dist >= 0.5, v=_g(f, "dist_high252"))]
    comps["trend"] = ComponentScore("trend", w["trend"] * s, w["trend"], rs)

    r126, r252, pers = _g(f, "ret126"), _g(f, "ret252"), _g(f, "persist6m")
    rsi_s = trap(_g(f, "rsi14"), 35, 48, 72, 85)
    s = 0.35 * lin(r126, 0.0, 0.5) + 0.25 * lin(r252, 0.0, 0.8) + 0.25 * lin(pers, 0.33, 0.83) + 0.15 * rsi_s
    rs = [R("mom_ret", r126 > 0 if not math.isnan(r126) else None, r126=r126, r252=r252),
          R("mom_persist", pers >= 0.5 if not math.isnan(pers) else None, v=pers), R("mom_rsi", None, v=_g(f, "rsi14"))]
    comps["momentum"] = ComponentScore("momentum", w["momentum"] * s, w["momentum"], rs)

    acc = 0.5 * lin(_g(f, "obv_trend60"), -0.1, 0.3) + 0.5 * lin(_g(f, "updown60"), 0.85, 1.4)
    dv = _g(f, "dollar_vol20")
    liq = lin(math.log10(dv) if dv and dv > 0 else float("nan"), 7.6, 9.0)
    s = 0.5 * acc + 0.3 * liq + 0.2 * trap(_g(f, "relvol"), 0.6, 0.9, 2.0, 4.0)
    rs = [R("vol_accum", acc >= 0.5, v=acc), R("vol_liq", liq >= 0.5, dv=dv), R("vol_rel", None, v=_g(f, "relvol"))]
    comps["volume"] = ComponentScore("volume", w["volume"] * s, w["volume"], rs)

    ranks = {"new_high": 0.9, "lt_pullback": 1.0, "volatility_contraction": 0.7, "reclaim_sma50": 0.6}
    present = [k for k in ranks if flags.get(k)]
    base = max((ranks[k] for k in present), default=0.0)
    in_trend = _gt(c, _g(f, "sma200")) and _gt(_g(f, "sma50"), _g(f, "sma200"))
    s = min(1.0, base + (0.1 if len(present) >= 2 else 0.0)) if present else (0.3 if in_trend else 0.0)
    rs = [R("setup_present", True, name=k) for k in present] or \
         [R("setup_trend_only", None) if in_trend else R("setup_none", False)]
    comps["setup"] = ComponentScore("setup", w["setup"] * s, w["setup"], rs)

    r126s, r252s, secs, line = _g(f, "rs126_spy"), _g(f, "rs252_spy"), _g(f, "rs126_sec"), _g(f, "rsline_spy")
    s = (0.35 * lin(r126s, -0.05, 0.30) + 0.25 * lin(r252s, -0.05, 0.50)
         + 0.20 * lin(secs, -0.05, 0.20) + 0.20 * lin(line, -0.03, 0.15))
    rs = [R("rs_spy", r126s > 0 if not math.isnan(r126s) else None, v=r126s),
          R("rs_sector", secs > 0 if not math.isnan(secs) else None, v=secs), R("rs_line", line > 0 if not math.isnan(line) else None, v=line)]
    comps["relative_strength"] = ComponentScore("relative_strength", w["relative_strength"] * s, w["relative_strength"], rs)

    ap, rv60, mdd = _g(f, "atr_pct"), _g(f, "rv60"), _g(f, "maxdd126")
    s = 0.5 * trap(ap, 0.005, 0.01, 0.03, 0.06) + 0.3 * trap(rv60, 0.12, 0.20, 0.40, 0.70) + 0.2 * lin(mdd, -0.45, -0.10)
    rs = [R("volat_atr", 0.01 <= ap <= 0.05 if not math.isnan(ap) else None, v=ap), R("volat_dd", mdd > -0.25 if not math.isnan(mdd) else None, v=mdd)]
    comps["volatility"] = ComponentScore("volatility", w["volatility"] * s, w["volatility"], rs)

    comps["risk_reward"] = _rr_component(lv, w, stop_best=(2.0, 5.0))
    return comps


# ---------------------------------------------------------------- validation rules
def validate_strategy(strategy: str, f: Mapping, flags: Mapping, lv: TradeLevels | None, cfg: ScanCfg) -> list[Reason]:
    """Strategy-specific go/no-go rules; any failure => not validated (kept out of Top lists)."""
    out: list[Reason] = []
    c, ap = _g(f, "close"), _g(f, "atr_pct")

    def need(cond: bool, code: str, **p):
        if not cond:
            out.append(R(code, False, **p))

    if lv is None:
        out.append(R("val_no_levels", False))
    if strategy == SWING:
        need(_gt(c, _g(f, "sma50")), "val_below_sma50")
        need(0.01 <= ap <= 0.08 if not math.isnan(ap) else False, "val_atr_range", v=ap)
        need(_g(f, "rsi14", 100) < 80, "val_overbought", v=_g(f, "rsi14"))
        need(not _gt(c - _g(f, "sma20"), 3.5 * _g(f, "atr14", 0)) if not math.isnan(_g(f, "sma20")) else False, "val_extended")
        need(any(flags.get(k) for k in ("breakout", "near_breakout", "pullback_support", "sma20_reclaim",
                                        "tight_consolidation", "volatility_contraction")), "val_no_setup")
        if flags.get("breakout"):
            need(_g(f, "relvol", 0) >= 1.3, "val_breakout_volume", v=_g(f, "relvol"))
        if lv is not None:
            need(lv.rr >= cfg.swing_min_rr, "val_min_rr", rr=lv.rr, need=cfg.swing_min_rr)
    else:
        need(_gt(c, _g(f, "sma200")), "val_below_sma200")
        need(_gt(_g(f, "sma50"), _g(f, "sma200")), "val_sma50_below_200")
        s200 = _g(f, "sma200_chg20")
        need(not math.isnan(s200) and s200 >= 0, "val_sma200_falling", v=s200)
        need(_g(f, "dist_high252", -1) > -0.35, "val_deep_drawdown", v=_g(f, "dist_high252"))
        need(ap <= 0.06 if not math.isnan(ap) else False, "val_atr_high", v=ap)
        need(_g(f, "rs126_spy", -1) > 0, "val_rs_negative", v=_g(f, "rs126_spy"))
        need(_g(f, "dollar_vol20", 0) >= cfg.longterm_min_dollar_volume, "val_lt_liquidity")
        if lv is not None:
            need(lv.rr >= cfg.longterm_min_rr, "val_min_rr", rr=lv.rr, need=cfg.longterm_min_rr)
    return out


# ---------------------------------------------------------------- public API
def score_row(strategy: str, f: Mapping, flags: Mapping, hist: pd.DataFrame | None, cfg: ScanCfg,
              experimental: list[str] | None = None) -> ScoreResult:
    """Score one row (`f` = indicator-frame row as mapping, `flags` = core pattern flags at that row)."""
    w = cfg.swing_weights if strategy == SWING else cfg.longterm_weights
    lv = compute_levels(strategy, f, hist)
    comps = (_swing if strategy == SWING else _longterm)(f, flags, lv, w)
    total = float(sum(c.points for c in comps.values()))
    failures = validate_strategy(strategy, f, flags, lv, cfg)
    return ScoreResult(strategy=strategy, total=total, klass=klass_for(total, cfg), validated=not failures,
                       failures=failures, components=comps, levels=lv,
                       setups=[k for k in CORE if flags.get(k)], experimental=experimental or [],
                       version=SCORING_VERSION)
