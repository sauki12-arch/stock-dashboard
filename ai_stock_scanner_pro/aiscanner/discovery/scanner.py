"""Layer 3 orchestrator: load data → indicators → gates → scoring → regime → persisted ScanResult."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .. import SCORING_VERSION, __version__
from ..brain import regime as brain
from ..config import Settings
from ..data import market_calendar as cal
from ..data.engine import DataEngine
from ..data.store import Store, dumps
from ..data.universe import SECTOR_ETFS, Universe
from ..data.validation import corwin_schultz_spread
from ..models import LONGTERM, STRATEGIES, SWING, R, Reason
from .indicators import build_frame
from .patterns import core_flags, detect_experimental
from .scoring import check_eligibility, score_row

log = logging.getLogger(__name__)
SNAP_KEYS = ["close", "sma20", "sma50", "sma200", "ema20", "ema50", "rsi14", "macd_hist", "atr14", "atr_pct", "relvol",
             "dollar_vol20", "ret20", "ret63", "ret126", "ret252", "dist_high252", "rs63_spy", "rs126_spy", "rv20",
             "high50_prev", "low50_prev", "bb_width_pctile"]
LOOKBACK_BARS = 520


@dataclass
class Candidate:
    symbol: str
    sector: str
    price: float
    last_bar: str
    eligible: bool
    gate_failures: list[dict] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    scores: dict[str, dict] = field(default_factory=dict)      # strategy -> ScoreResult.to_dict()

    def score(self, strategy: str) -> float:
        return float(self.scores.get(strategy, {}).get("total", 0.0))

    def validated(self, strategy: str) -> bool:
        return bool(self.eligible and self.scores.get(strategy, {}).get("validated", False))


@dataclass
class ScanResult:
    as_of: str
    ts: str
    regime: dict
    candidates: list[Candidate]
    failed: dict[str, str]
    provider: str
    synthetic: bool
    n_universe: int
    version: str = SCORING_VERSION
    bands_calibrated: bool = False
    stale_symbols: list[str] = field(default_factory=list)
    run_id: int | None = None
    seconds: float = 0.0

    @property
    def n_eligible(self) -> int:
        return sum(1 for c in self.candidates if c.eligible)

    def ranked(self, strategy: str, n: int | None = None, include_unvalidated: bool = False) -> list[Candidate]:
        rows = [c for c in self.candidates if c.eligible and strategy in c.scores
                and (include_unvalidated or c.validated(strategy))]

        def key(c: Candidate):
            lv = c.scores[strategy].get("levels") or {}
            return (-c.score(strategy), -(lv.get("rr") or 0.0), -(c.metrics.get("dollar_vol20") or 0.0), c.symbol)
        rows.sort(key=key)
        return rows[:n] if n else rows

    def by_symbol(self, sym: str) -> Candidate | None:
        return next((c for c in self.candidates if c.symbol == sym), None)


class Scanner:
    def __init__(self, engine: DataEngine, store: Store, universe: Universe, settings: Settings):
        self.engine, self.store, self.universe, self.settings = engine, store, universe, settings

    # ------------------------------------------------------------------
    def run(self, progress=None, cancel=None, persist: bool = True) -> ScanResult:
        t0 = time.time()
        cfg = self.settings.scan
        now = self.engine.clock()
        support_raw = {s: self.engine.load(s)[0] for s in self.universe.support_symbols()}
        spy_df = support_raw.get("SPY")
        spy_ok = spy_df is not None and len(spy_df) > 0
        spy_f = build_frame(spy_df.tail(LOOKBACK_BARS)) if spy_ok else None
        qqq_f = build_frame(support_raw["QQQ"].tail(LOOKBACK_BARS), spy_df) if len(support_raw.get("QQQ", [])) else None
        vix_f = support_raw["^VIX"].tail(LOOKBACK_BARS) if len(support_raw.get("^VIX", [])) else None
        sector_df = {e: support_raw[e] for e in SECTOR_ETFS if len(support_raw.get(e, [])) > 0}
        sector_f = {e: build_frame(d.tail(LOOKBACK_BARS), spy_df if spy_ok else None) for e, d in sector_df.items()}

        cands: list[Candidate] = []
        failed: dict[str, str] = {}
        stale: list[str] = []
        snaps: list[dict] = []
        rets: dict[str, pd.Series] = {}
        liq: dict[str, float] = {}
        total = len(self.universe.symbols)
        for i, sym in enumerate(self.universe.symbols):
            if cancel and cancel():
                break
            if progress:
                progress(i, total, sym)
            df, q = self.engine.load(sym, min_bars=1)
            if df.empty:
                failed[sym] = "no data"
                continue
            if q.stale:
                stale.append(sym)
            df = df.tail(LOOKBACK_BARS)
            etf = self.universe.sector.get(sym, "")
            try:
                c = self._analyse(sym, etf, df, q, spy_df if spy_ok else None, sector_df.get(etf), snaps, rets, liq)
            except Exception as e:  # noqa: BLE001 - one bad symbol must never kill the scan
                log.exception("scan failed for %s", sym)
                failed[sym] = f"analysis error: {type(e).__name__}: {e}"
                continue
            cands.append(c)
        if progress:
            progress(total, total, "regime")

        top_liq = sorted(liq, key=liq.get, reverse=True)[:60]
        ret_df = pd.DataFrame({s: rets[s] for s in top_liq}) if top_liq else None
        breadth = brain.compute_breadth(snaps, ret_df)
        regime = brain.assess(spy_f, qqq_f, vix_f if vix_f is None else pd.DataFrame({"close": vix_f["close"]}),
                              sector_f, breadth)
        res = ScanResult(as_of=(cands[0].last_bar if cands else ""), ts=now.isoformat(timespec="seconds"),
                         regime=regime.to_dict(), candidates=cands, failed=failed,
                         provider=self.engine.provider.name, synthetic=self.engine.provider.is_synthetic,
                         n_universe=total, bands_calibrated=cfg.bands_calibrated, stale_symbols=stale,
                         seconds=time.time() - t0)
        res.as_of = cal.last_completed_session(now, self.engine.cfg.grace_minutes_after_close).isoformat()
        if persist and not (cancel and cancel()):
            res.run_id = save_scan(self.store, res)
        return res

    def _analyse(self, sym, etf, df, q, spy_df, sec_df, snaps, rets, liq) -> Candidate:
        cfg = self.settings.scan
        f = build_frame(df, spy_df, sec_df)
        flags = core_flags(f)
        row = f.iloc[-1]
        frow = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
        flag_row = {k: bool(v) for k, v in flags.iloc[-1].items()}
        spread = corwin_schultz_spread(df)
        gates = check_eligibility({k: (np.nan if v is None else v) for k, v in frow.items()}, len(df), q.ok, q.codes(), spread, cfg)
        cand = Candidate(symbol=sym, sector=etf, price=float(df["close"].iloc[-1]), last_bar=df.index[-1].strftime("%Y-%m-%d"),
                         eligible=not gates, gate_failures=[g.to_dict() for g in gates], quality=q.codes(),
                         metrics={k: frow.get(k) for k in SNAP_KEYS} | {"spread_proxy": spread})
        if not gates:
            snaps.append({k: (np.nan if v is None else v) for k, v in frow.items()})
            rets[sym] = df["close"].pct_change().tail(70)
            liq[sym] = frow.get("dollar_vol20") or 0.0
            fr = {k: (np.nan if v is None else v) for k, v in frow.items()}
            sw = score_row(SWING, fr, flag_row, df, cfg)
            lt = score_row(LONGTERM, fr, flag_row, df, cfg)
            if cfg.enable_experimental_patterns and (sw.total >= cfg.band_monitor or lt.total >= cfg.band_monitor):
                exp = [h.name for h in detect_experimental(df)]
                sw.experimental = lt.experimental = exp
            cand.scores = {SWING: sw.to_dict(), LONGTERM: lt.to_dict()}
        return cand


# ---------------------------------------------------------------- persistence
def save_scan(store: Store, res: ScanResult) -> int:
    with store.conn() as c:
        cur = c.execute(
            "INSERT INTO scan_runs(ts,as_of,regime,n_universe,n_eligible,provider,synthetic,version,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (res.ts, res.as_of, res.regime.get("regime"), res.n_universe, res.n_eligible, res.provider,
             int(res.synthetic), res.version,
             dumps({"regime": res.regime, "failed": res.failed, "stale": res.stale_symbols,
                    "bands_calibrated": res.bands_calibrated, "seconds": res.seconds})))
        rid = cur.lastrowid
        rows = []
        for cd in res.candidates:
            payload = {"symbol": cd.symbol, "sector": cd.sector, "price": cd.price, "last_bar": cd.last_bar,
                       "gate_failures": cd.gate_failures, "quality": cd.quality, "metrics": cd.metrics}
            if not cd.scores:
                rows.append((rid, cd.symbol, "-", 0.0, "low", 0, int(cd.eligible), dumps(payload)))
            for st, sc in cd.scores.items():
                rows.append((rid, cd.symbol, st, sc["total"], sc["klass"], int(sc["validated"]), int(cd.eligible),
                             dumps(payload | {"score": sc})))
        c.executemany("INSERT OR REPLACE INTO scan_results VALUES(?,?,?,?,?,?,?,?)", rows)
    res.run_id = rid
    return rid


def load_scan(store: Store, run_id: int | None = None, offset: int = 0) -> ScanResult | None:
    """Load a stored run (latest by default; offset=1 gives the one before the latest)."""
    with store.conn() as c:
        if run_id is None:
            r = c.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1 OFFSET ?", (offset,)).fetchone()
        else:
            r = c.execute("SELECT * FROM scan_runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        rows = c.execute("SELECT * FROM scan_results WHERE run_id=?", (r["id"],)).fetchall()
    meta = json.loads(r["payload_json"])
    by: dict[str, Candidate] = {}
    for row in rows:
        p = json.loads(row["payload_json"])
        cd = by.get(row["symbol"])
        if cd is None:
            cd = by[row["symbol"]] = Candidate(symbol=p["symbol"], sector=p["sector"], price=p["price"],
                                               last_bar=p["last_bar"], eligible=bool(row["eligible"]),
                                               gate_failures=p["gate_failures"], quality=p["quality"], metrics=p["metrics"])
        if "score" in p:
            cd.scores[row["strategy"]] = p["score"]
    return ScanResult(as_of=r["as_of"], ts=r["ts"], regime=meta["regime"], candidates=list(by.values()),
                      failed=meta["failed"], provider=r["provider"], synthetic=bool(r["synthetic"]),
                      n_universe=r["n_universe"], version=r["version"], bands_calibrated=meta.get("bands_calibrated", False),
                      stale_symbols=meta.get("stale", []), run_id=r["id"], seconds=meta.get("seconds", 0.0))
