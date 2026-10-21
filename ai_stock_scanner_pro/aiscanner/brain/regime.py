"""Layer 2 – deterministic market-regime engine.

Transparent weighted composite of SPY/QQQ trend, universe breadth, volatility (VIX if present, else
realised vol/ATR), broad-market drawdown and cross-sectional correlation. Missing inputs are dropped and the
remaining weights renormalised; coverage lowers confidence. No LLM is involved: explanations are templates over
the computed numbers, so nothing here can invent a market fact.

Calibration: thresholds are PROVISIONAL (`calibration="uncalibrated"`) until a walk-forward calibration record
is approved. Confidence is an agreement/margin heuristic, **not** a probability.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..discovery.scoring import lin
from ..models import NEUTRAL, R, RISK_OFF, RISK_ON, Reason

WEIGHTS = {"trend_spy": 0.25, "trend_qqq": 0.10, "breadth": 0.25, "volatility": 0.15,
           "drawdown": 0.15, "correlation": 0.10}
RISK_ON_AT, RISK_OFF_AT = 62.0, 40.0


@dataclass
class Breadth:
    n: int = 0
    pct_above_sma50: float = float("nan")
    pct_above_sma200: float = float("nan")
    new_high_minus_low: float = float("nan")      # (share at 50d highs) - (share at 50d lows)
    avg_correlation: float = float("nan")


@dataclass
class RegimeAssessment:
    regime: str
    score: float
    confidence: float
    calibration: str
    components: dict[str, float]                  # 0..1 each (1 = risk-on supportive)
    missing: list[str]
    strongest_sectors: list[tuple[str, float]]
    weakest_sectors: list[tuple[str, float]]
    posture: str                                  # full | selective | defensive
    suggested_max_positions: int                  # ADVISORY ONLY – hard limits live in RiskEngine
    evidence: list[Reason] = field(default_factory=list)
    counterarguments: list[Reason] = field(default_factory=list)
    invalidations: list[Reason] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    override: str = ""

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("regime", "score", "confidence", "calibration", "components", "missing",
                                           "strongest_sectors", "weakest_sectors", "posture",
                                           "suggested_max_positions", "metrics", "override")}
        for k in ("evidence", "counterarguments", "invalidations"):
            d[k] = [r.to_dict() for r in getattr(self, k)]
        return d


def _last(f: pd.DataFrame, col: str) -> float:
    return float(f[col].iloc[-1]) if f is not None and len(f) and col in f else float("nan")


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def compute_breadth(snapshots: list[dict], returns: pd.DataFrame | None = None, corr_sample: int = 60) -> Breadth:
    """snapshots: last-row indicator dicts of eligible stocks. returns: DataFrame of daily returns (cols=symbols)."""
    b = Breadth(n=len(snapshots))
    if not snapshots:
        return b
    df = pd.DataFrame(snapshots)

    def share(cond: pd.Series, valid: pd.Series) -> float:
        return float(cond[valid].mean()) if valid.sum() else float("nan")
    v50, v200 = df["sma50"].notna(), df["sma200"].notna()
    b.pct_above_sma50 = share(df["close"] > df["sma50"], v50)
    b.pct_above_sma200 = share(df["close"] > df["sma200"], v200)
    hi = share(df["close"] >= df["high50_prev"], df["high50_prev"].notna())
    lo = share(df["close"] <= df["low50_prev"], df["low50_prev"].notna()) if "low50_prev" in df else float("nan")
    b.new_high_minus_low = hi - lo if not (_nan(hi) or _nan(lo)) else float("nan")
    if returns is not None and returns.shape[1] >= 5:
        r = returns.tail(60).dropna(axis=1, thresh=50).iloc[:, :corr_sample]
        if r.shape[1] >= 5:
            cm = r.corr().to_numpy()
            iu = np.triu_indices_from(cm, k=1)
            b.avg_correlation = float(np.nanmean(cm[iu]))
    return b


def assess(spy: pd.DataFrame | None, qqq: pd.DataFrame | None, vix: pd.DataFrame | None,
           sector_frames: dict[str, pd.DataFrame], breadth: Breadth | None) -> RegimeAssessment:
    """Inputs are indicator frames (`build_frame` output) – last row is the decision date."""
    comp: dict[str, float] = {}
    ev: list[Reason] = []
    metrics: dict = {}

    def trend_score(f: pd.DataFrame) -> float:
        c = _last(f, "close")
        parts = [(0.4, c > _last(f, "sma200")), (0.2, _last(f, "sma50") > _last(f, "sma200")),
                 (0.2, c > _last(f, "sma50"))]
        s = sum(w * float(bool(ok)) for w, ok in parts if not _nan(_last(f, "sma200")))
        return s + 0.2 * lin(_last(f, "sma50_chg10"), -0.02, 0.03)

    if spy is not None and len(spy) >= 200 and not _nan(_last(spy, "sma200")):
        comp["trend_spy"] = trend_score(spy)
        metrics.update(spy_close=_last(spy, "close"), spy_sma50=_last(spy, "sma50"), spy_sma200=_last(spy, "sma200"))
        ev.append(R("rg_spy_vs_200", metrics["spy_close"] > metrics["spy_sma200"], close=metrics["spy_close"], sma=metrics["spy_sma200"]))
        ev.append(R("rg_spy_vs_50", metrics["spy_close"] > metrics["spy_sma50"], close=metrics["spy_close"], sma=metrics["spy_sma50"]))
    if qqq is not None and len(qqq) >= 200 and not _nan(_last(qqq, "sma200")):
        comp["trend_qqq"] = trend_score(qqq)
        ev.append(R("rg_qqq_vs_200", _last(qqq, "close") > _last(qqq, "sma200"), close=_last(qqq, "close"), sma=_last(qqq, "sma200")))

    if breadth is not None and breadth.n >= 20 and not _nan(breadth.pct_above_sma50):
        parts, ws = [], []
        parts.append(lin(breadth.pct_above_sma50, 0.30, 0.70)); ws.append(0.5)
        if not _nan(breadth.pct_above_sma200):
            parts.append(lin(breadth.pct_above_sma200, 0.35, 0.70)); ws.append(0.3)
        if not _nan(breadth.new_high_minus_low):
            parts.append(lin(breadth.new_high_minus_low, -0.10, 0.20)); ws.append(0.2)
        comp["breadth"] = float(np.average(parts, weights=ws))
        metrics.update(pct50=breadth.pct_above_sma50, pct200=breadth.pct_above_sma200, n_breadth=breadth.n)
        ev.append(R("rg_breadth50", breadth.pct_above_sma50 >= 0.5, v=breadth.pct_above_sma50, n=breadth.n))
        if not _nan(breadth.pct_above_sma200):
            ev.append(R("rg_breadth200", breadth.pct_above_sma200 >= 0.5, v=breadth.pct_above_sma200))

    vix_v = _last(vix, "close") if vix is not None and len(vix) else float("nan")
    if spy is not None and len(spy) >= 60:
        rv, ap = _last(spy, "rv20"), _last(spy, "atr_pct")
        parts = [1 - lin(rv, 0.10, 0.35), 1 - lin(ap, 0.007, 0.025)]
        if not _nan(vix_v):
            parts.append(1 - lin(vix_v, 14, 32))
            metrics["vix"] = vix_v
            ev.append(R("rg_vix", vix_v < 20, v=vix_v))
        comp["volatility"] = float(np.mean([p for p in parts if not _nan(p)]))
        metrics.update(spy_rv20=rv, spy_atr_pct=ap)
        ev.append(R("rg_realized_vol", rv < 0.18, v=rv))

    if spy is not None and len(spy) >= 120:
        dd = _last(spy, "dist_high252")
        if not _nan(dd):
            comp["drawdown"] = 1 - lin(-dd, 0.03, 0.20)
            metrics["spy_drawdown"] = dd
            ev.append(R("rg_drawdown", dd > -0.05, v=dd))

    if breadth is not None and not _nan(breadth.avg_correlation):
        comp["correlation"] = 1 - lin(breadth.avg_correlation, 0.25, 0.70)
        metrics["avg_corr"] = breadth.avg_correlation
        ev.append(R("rg_correlation", breadth.avg_correlation < 0.45, v=breadth.avg_correlation))

    missing = [k for k in WEIGHTS if k not in comp]
    wsum = sum(WEIGHTS[k] for k in comp)
    coverage = wsum / sum(WEIGHTS.values())
    if not comp or wsum < 0.35:
        return RegimeAssessment(NEUTRAL, 50.0, 0.0, "uncalibrated", comp, missing, [], [], "defensive", 3,
                                evidence=[R("rg_insufficient_data", False)], metrics=metrics, override="insufficient_data")
    score = 100 * sum(WEIGHTS[k] * comp[k] for k in comp) / wsum
    regime = RISK_ON if score >= RISK_ON_AT else RISK_OFF if score <= RISK_OFF_AT else NEUTRAL

    override = ""
    dd = metrics.get("spy_drawdown", 0.0)
    if "spy_sma200" in metrics:
        below200 = metrics["spy_close"] < metrics["spy_sma200"]
        if below200 and dd <= -0.15:
            regime, override = RISK_OFF, "deep_drawdown_below_200"
        elif regime == RISK_ON and below200:
            regime, override = NEUTRAL, "risk_on_blocked_below_200"

    # agreement of components with the final regime
    def agrees(v: float) -> bool:
        return v >= 0.6 if regime == RISK_ON else v <= 0.4 if regime == RISK_OFF else 0.35 < v < 0.65
    agree_w = sum(WEIGHTS[k] for k, v in comp.items() if agrees(v)) / wsum
    margin = min(1.0, min(abs(score - RISK_ON_AT), abs(score - RISK_OFF_AT)) / 15) if regime == NEUTRAL else \
        min(1.0, abs(score - (RISK_ON_AT if regime == RISK_ON else RISK_OFF_AT)) / 15)
    confidence = round(coverage * (0.5 * agree_w + 0.5 * margin), 3)

    counter = [r for r in ev if r.positive is not None and ((r.positive and regime == RISK_OFF) or (not r.positive and regime == RISK_ON))]
    if regime == NEUTRAL:
        counter = [r for r in ev if r.positive is not None]
    evidence = [r for r in ev if r not in counter]

    inv: list[Reason] = []
    if regime == RISK_ON:
        inv += [R("inv_spy_below", None, level=metrics.get("spy_sma50")), R("inv_spy_below200", None, level=metrics.get("spy_sma200")),
                R("inv_vix_above", None, level=25.0), R("inv_breadth_below", None, level=0.45)]
    elif regime == RISK_OFF:
        inv += [R("inv_spy_reclaim", None, level=metrics.get("spy_sma200")), R("inv_breadth_above", None, level=0.55)]
    else:
        inv += [R("inv_up_to_on", None, level=RISK_ON_AT), R("inv_down_to_off", None, level=RISK_OFF_AT)]

    # sector relative strength vs SPY
    secs: list[tuple[str, float]] = []
    for sym, sf in sector_frames.items():
        if sf is None or len(sf) < 130 or spy is None:
            continue
        r = 0.5 * _last(sf, "rs63_spy") + 0.3 * _last(sf, "rs20_spy") + 0.2 * _last(sf, "rs126_spy")
        if not _nan(r):
            secs.append((sym, float(r)))
    secs.sort(key=lambda t: -t[1])
    posture, maxpos = {RISK_ON: ("full", 10), NEUTRAL: ("selective", 6), RISK_OFF: ("defensive", 3)}[regime]
    return RegimeAssessment(regime, round(score, 1), confidence, "uncalibrated", {k: round(v, 3) for k, v in comp.items()},
                            missing, secs[:3], list(reversed(secs[-3:])), posture, maxpos, evidence, counter, inv,
                            metrics, override)
