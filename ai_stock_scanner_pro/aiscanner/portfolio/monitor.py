"""Portfolio monitor: per-position health, ATR trailing-stop proposals, portfolio breaches → Alert objects.

Severity policy (only CRITICAL is pushed to Telegram, everything is kept in Alerts History):
  critical: stop breached · confirmed trend break · drawdown/aggregate-risk/sector limit breached
  warning : near stop · early trend weakness · position over size cap · high correlation · stale data
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from .. import i18n
from ..config import RiskCfg
from ..data.validation import DataQuality
from ..models import CRITICAL, INFO, SWING, WARNING, Alert, R, Reason
from .risk import PortfolioState, RiskEngine, open_risk_of
from .store import Position


@dataclass
class PositionHealth:
    symbol: str
    qty: float
    avg_cost: float
    price: float
    market_value: float
    pnl: float
    pnl_pct: float
    stop: float | None
    target: float | None
    r_multiple: float | None             # (price - avg_cost) / (avg_cost - stop) if stop below cost
    dist_to_stop_atr: float | None
    open_risk: float
    risk_assumed: bool
    trailing_stop: float | None          # PROPOSAL (never auto-applied)
    status: str                          # ok | warning | critical | no_data
    flags: list[Reason] = field(default_factory=list)
    sector: str = ""
    strategy: str = ""


@dataclass
class MonitorReport:
    positions: list[PositionHealth]
    metrics: dict
    alerts: list[Alert]
    data_warnings: list[str] = field(default_factory=list)


def _f(row, k):
    v = row.get(k) if hasattr(row, "get") else None
    return float("nan") if v is None else float(v)


def chandelier_stop(frame: pd.DataFrame, opened_at: str, mult: float = 3.0, max_lookback: int = 60) -> float | None:
    """highest close since entry (or last `max_lookback` bars) − mult × ATR, capped at close − 0.5 ATR."""
    if frame is None or len(frame) < 20 or "atr14" not in frame:
        return None
    sub = frame.tail(max_lookback)
    if opened_at:
        try:
            ts = pd.Timestamp(opened_at)
            ts = ts.tz_localize(None) if ts.tzinfo else ts
            since = sub[sub.index >= ts.normalize()]
            if len(since) >= 1:
                sub = since
        except (ValueError, TypeError):
            pass
    atr = float(frame["atr14"].iloc[-1])
    close = float(frame["close"].iloc[-1])
    if not (atr > 0):
        return None
    return float(min(sub["close"].max() - mult * atr, close - 0.5 * atr))


class PortfolioMonitor:
    def __init__(self, risk: RiskEngine, cfg: RiskCfg):
        self.risk, self.cfg = risk, cfg

    def evaluate(self, st: PortfolioState, frames: dict[str, pd.DataFrame], quality: dict[str, DataQuality] | None = None,
                 peak_equity: float = 0.0, returns: pd.DataFrame | None = None,
                 live_prices: dict[str, float] | None = None) -> MonitorReport:
        quality = quality or {}
        live = live_prices or {}
        c = self.cfg
        health: list[PositionHealth] = []
        alerts: list[Alert] = []
        warns: list[str] = []
        for p in st.positions:
            fr = frames.get(p.symbol)
            q = quality.get(p.symbol)
            if fr is None or len(fr) == 0:
                health.append(PositionHealth(p.symbol, p.qty, p.avg_cost, p.avg_cost, p.qty * p.avg_cost, 0.0, 0.0, p.stop,
                                             p.target, None, None, *open_risk_of(p, p.avg_cost, c.assumed_risk_pct_no_stop),
                                             None, "no_data", [R("al_stale", False, symbol=p.symbol, detail="no data")],
                                             p.sector, p.strategy))
                warns.append(f"{p.symbol}: no market data")
                continue
            h, a = self._position(p, fr, q, live.get(p.symbol))
            health.append(h)
            alerts += a
            if q is not None and (q.stale or not q.ok):
                warns.append(f"{p.symbol}: {','.join(q.codes())}")
        metrics = self.risk.metrics(st, peak_equity, returns)
        alerts += self._portfolio_alerts(metrics, st)
        return MonitorReport(health, metrics, alerts, warns)

    # ------------------------------------------------------------------
    def _position(self, p: Position, fr: pd.DataFrame, q: DataQuality | None, live: float | None = None):
        row = fr.iloc[-1]
        price, atr = (live if live else _f(row, "close")), _f(row, "atr14")
        value = p.qty * price
        pnl = (price - p.avg_cost) * p.qty
        risk, assumed = open_risk_of(p, price, self.cfg.assumed_risk_pct_no_stop)
        rmult = None
        if p.stop and p.avg_cost > p.stop:
            rmult = (price - p.avg_cost) / (p.avg_cost - p.stop)
        dist_atr = (price - p.stop) / atr if p.stop and atr and atr > 0 else None
        flags: list[Reason] = []
        alerts: list[Alert] = []
        status = "ok"
        date = fr.index[-1].strftime("%Y-%m-%d")

        def raise_(sev: str, kind: str, reason: Reason, key: str):
            nonlocal status
            flags.append(reason)
            txt = i18n.render(reason)
            alerts.append(Alert(key=key, severity=sev, kind=kind, title=f"{p.symbol}: {i18n.SEVERITY[sev]}", body=txt, symbol=p.symbol))
            if sev == CRITICAL:
                status = "critical"
            elif status != "critical":
                status = "warning"

        if p.stop and price <= p.stop:
            raise_(CRITICAL, "stop_breach", R("al_stop_breach", False, symbol=p.symbol, price=price, stop=p.stop),
                   f"stop_breach:{p.symbol}:{p.stop:.4f}")
        elif dist_atr is not None and dist_atr <= 0.5:
            raise_(WARNING, "near_stop", R("al_near_stop", False, symbol=p.symbol, price=price, stop=p.stop, dist=dist_atr),
                   f"near_stop:{p.symbol}:{p.stop:.4f}")

        sma20, sma50, sma200, mh = _f(row, "sma20"), _f(row, "sma50"), _f(row, "sma200"), _f(row, "macd_hist")
        if p.strategy == SWING:
            if price < sma50 and mh < 0:
                raise_(CRITICAL, "trend_break", R("al_trend_break", False, symbol=p.symbol, detail="מתחת SMA50 + MACD שלילי"),
                       f"trend_break:{p.symbol}:swing:{date[:7]}")
            elif price < sma20:
                raise_(WARNING, "trend_weak", R("al_trend_break", False, symbol=p.symbol, detail="מתחת SMA20"),
                       f"trend_weak:{p.symbol}:swing:{date}")
        else:
            if price < sma200:
                raise_(CRITICAL, "trend_break", R("al_trend_break", False, symbol=p.symbol, detail="מתחת SMA200"),
                       f"trend_break:{p.symbol}:lt:{date[:7]}")
            elif price < sma50:
                raise_(WARNING, "trend_weak", R("al_trend_break", False, symbol=p.symbol, detail="מתחת SMA50"),
                       f"trend_weak:{p.symbol}:lt:{date[:7]}")

        if q is not None and (q.stale or not q.ok):
            flags.append(R("al_stale", False, symbol=p.symbol, detail=",".join(q.codes())))
            alerts.append(Alert(f"stale:{p.symbol}:{date}", WARNING, "stale_data", f"{p.symbol}: נתונים",
                                i18n.render(flags[-1]), p.symbol))
            if status == "ok":
                status = "warning"

        trail = chandelier_stop(fr, p.opened_at)
        if trail is not None and (p.stop is None or trail > p.stop) and trail < price:
            flags.append(R("al_trailing", None, symbol=p.symbol, new=trail, old=p.stop if p.stop else 0.0))
        else:
            trail = None
        pct = (price / p.avg_cost - 1) if p.avg_cost else 0.0
        h = PositionHealth(p.symbol, p.qty, p.avg_cost, price, value, pnl, pct, p.stop, p.target, rmult, dist_atr, risk,
                           assumed, trail, status, flags, p.sector, p.strategy)
        return h, alerts

    def _portfolio_alerts(self, m: dict, st: PortfolioState) -> list[Alert]:
        c, out = self.cfg, []
        if m["drawdown"] <= -c.drawdown_alert_pct:
            lvl = int(abs(m["drawdown"]) * 100 // 5) * 5            # re-alert every extra 5 %
            r = R("al_drawdown", False, dd=m["drawdown"], limit=c.drawdown_alert_pct)
            out.append(Alert(f"portfolio_dd:{lvl}", CRITICAL, "portfolio_drawdown", "סיכון תיק: ירידה מהשיא", i18n.render(r)))
        if m["open_risk_pct"] > c.max_total_risk_pct + 1e-12:
            r = R("al_total_risk", False, pct=m["open_risk_pct"], max=c.max_total_risk_pct)
            out.append(Alert("portfolio_open_risk", CRITICAL, "portfolio_risk", "סיכון תיק: סיכון פתוח מצטבר", i18n.render(r)))
        for sec, w in m["sector_weights"].items():
            if sec != "?" and w > c.max_sector_pct + 1e-12:
                r = R("al_sector", False, sector=sec, pct=w, max=c.max_sector_pct)
                out.append(Alert(f"sector_conc:{sec}", CRITICAL, "portfolio_concentration", "סיכון תיק: ריכוז סקטור", i18n.render(r)))
        for sym, w in m["weights"].items():
            if w > c.max_position_pct + 1e-12:
                r = R("al_position_size", False, symbol=sym, pct=w, max=c.max_position_pct)
                out.append(Alert(f"pos_size:{sym}", WARNING, "position_size", f"{sym}: משקל", i18n.render(r), sym))
        corr = m.get("avg_correlation", float("nan"))
        if not math.isnan(corr) and corr > c.correlation_alert:
            r = R("al_correlation", False, v=corr, limit=c.correlation_alert)
            out.append(Alert("portfolio_corr", WARNING, "correlation", "קורלציה גבוהה בתיק", i18n.render(r)))
        return out
