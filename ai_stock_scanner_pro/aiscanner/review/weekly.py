"""Weekly self-review: compare past forecasts with realised outcomes and PROPOSE (never apply) scoring changes.

Safety: nothing here modifies settings or scoring. Proposals are stored with approved=0 and can only be approved
by an explicit call that requires an attached backtest summary (`ScoreVersions.approve`)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..config import WEIGHT_KEYS
from ..data import market_calendar as cal
from ..data.store import Store, dumps
from ..models import STRATEGIES

HORIZON = {"swing": 10, "longterm": 20}
MIN_SAMPLE = 30
MIN_SCORE = 70.0


@dataclass
class Outcome:
    symbol: str
    strategy: str
    run_id: int
    as_of: str
    score: float
    klass: str
    entry: float
    stop: float
    target: float
    result: str                 # target | stop | open | no_data
    fwd_return: float | None
    bars: int
    spy_return: float | None
    comps: dict[str, float]     # component points / max


@dataclass
class WeeklyReport:
    generated: str
    lookback_runs: int
    outcomes: list[Outcome]
    summary: dict
    regime_errors: list[dict]
    false_positives: list[dict]
    suggestions: list[dict]
    notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        L = [f"# סקירה שבועית – {self.generated}", "", "> דוח מידע בלבד. לא משנה אוטומטית את מודל הציונים. כל שינוי חייב גרסה, בקטסט ואישור ידני.", ""]
        for st, s in self.summary.items():
            L += [f"## {st}", f"- אותות שנבדקו: {s['n']} (הושלמו: {s['n_resolved']})",
                  f"- יעד לפני סטופ: {s['target_rate']:.0%} | סטופ לפני יעד: {s['stop_rate']:.0%}" if s["n_resolved"] else "- אין מספיק תוצאות שהושלמו",
                  f"- תשואה ממוצעת קדימה: {s['avg_fwd']:+.2%} (SPY באותן תקופות: {s['avg_spy']:+.2%})" if s["avg_fwd"] is not None else "", ""]
        L += ["## טעויות משטר שוק", *(f"- {e['as_of']}: {e['regime']} → SPY {e['spy_fwd']:+.1%}" for e in self.regime_errors), ""] if self.regime_errors else ["## טעויות משטר שוק", "- לא זוהו", ""]
        L += ["## חיוביים שגויים (נעצרו בסטופ)", *(f"- {f['as_of']} {f['symbol']} ({f['strategy']}) ציון {f['score']:.0f}" for f in self.false_positives[:20]), ""]
        L += ["## הצעות להתאמת ציונים (דורשות בקטסט ואישור)", *(f"- {s['text']}" for s in self.suggestions), ""]
        L += ["## הערות", *(f"- {n}" for n in self.notes)]
        return "\n".join(x for x in L if x is not None)


def _forward(bars: pd.DataFrame, as_of: str, n: int, entry: float, stop: float, target: float):
    fut = bars[bars.index > pd.Timestamp(as_of)].head(n)
    if fut.empty:
        return "no_data", None, 0
    for k, (_, r) in enumerate(fut.iterrows(), start=1):
        if r["low"] <= stop:
            return "stop", stop / entry - 1, k
        if r["high"] >= target:
            return "target", target / entry - 1, k
    return "open", float(fut["close"].iloc[-1] / entry - 1), len(fut)       # neither level touched within the horizon so far


def collect_outcomes(store: Store, since: date, now: datetime, min_score: float = MIN_SCORE) -> tuple[list[Outcome], list[dict], int]:
    with store.conn() as c:
        runs = c.execute("SELECT id, as_of, regime, synthetic FROM scan_runs WHERE as_of>=? ORDER BY id", (since.isoformat(),)).fetchall()
        outs: list[Outcome] = []
        spy = store.load_bars("SPY")
        regime_rows = []
        for r in runs:
            if r["as_of"] and not spy.empty:
                fut = spy[spy.index > pd.Timestamp(r["as_of"])].head(10)
                base = spy[spy.index <= pd.Timestamp(r["as_of"])]
                if len(fut) >= 5 and len(base):
                    regime_rows.append({"as_of": r["as_of"], "regime": r["regime"], "spy_fwd": float(fut["close"].iloc[-1] / base["close"].iloc[-1] - 1)})
            rows = c.execute("SELECT symbol,strategy,score,klass,payload_json FROM scan_results WHERE run_id=? AND validated=1 AND eligible=1 AND score>=?",
                             (r["id"], min_score)).fetchall()
            for row in rows:
                sc = json.loads(row["payload_json"]).get("score", {})
                lv = sc.get("levels")
                if not lv:
                    continue
                bars = store.load_bars(row["symbol"])
                res, fwd, k = _forward(bars, r["as_of"], HORIZON[row["strategy"]], lv["entry"], lv["stop"], lv["target"])
                spy_ret = None
                if not spy.empty and k:
                    f2 = spy[spy.index > pd.Timestamp(r["as_of"])].head(k)
                    b2 = spy[spy.index <= pd.Timestamp(r["as_of"])]
                    if len(f2) and len(b2):
                        spy_ret = float(f2["close"].iloc[-1] / b2["close"].iloc[-1] - 1)
                comps = {k_: v["points"] / v["max"] if v["max"] else 0.0 for k_, v in sc.get("components", {}).items()}
                outs.append(Outcome(row["symbol"], row["strategy"], r["id"], r["as_of"], row["score"], row["klass"], lv["entry"],
                                    lv["stop"], lv["target"], res, fwd, k, spy_ret, comps))
    return outs, regime_rows, len(runs)


def build_report(store: Store, now: datetime, lookback_weeks: int = 8) -> WeeklyReport:
    since = (now - timedelta(weeks=lookback_weeks)).date()
    outs, regime_rows, n_runs = collect_outcomes(store, since, now)
    summary: dict = {}
    notes: list[str] = []
    for st in STRATEGIES:
        o = [x for x in outs if x.strategy == st]
        resolved = [x for x in o if x.result in ("target", "stop")]
        fwd = [x.fwd_return for x in o if x.fwd_return is not None]
        spy = [x.spy_return for x in o if x.spy_return is not None]
        summary[st] = {"n": len(o), "n_resolved": len(resolved),
                       "target_rate": sum(x.result == "target" for x in resolved) / len(resolved) if resolved else 0.0,
                       "stop_rate": sum(x.result == "stop" for x in resolved) / len(resolved) if resolved else 0.0,
                       "avg_fwd": float(np.mean(fwd)) if fwd else None, "avg_spy": float(np.mean(spy)) if spy else None}
    errs = [e for e in regime_rows if (e["regime"] == "RISK_ON" and e["spy_fwd"] < -0.03) or (e["regime"] == "RISK_OFF" and e["spy_fwd"] > 0.03)]
    fps = [{"as_of": x.as_of, "symbol": x.symbol, "strategy": x.strategy, "score": x.score} for x in outs if x.result == "stop"]
    suggestions = _suggest(outs, notes)
    if n_runs == 0:
        notes.append("אין ריצות סריקה בתקופה – אין מה לסקור")
    return WeeklyReport(now.isoformat(timespec="seconds"), n_runs, outs, summary, errs, fps, suggestions, notes)


def _suggest(outs: list[Outcome], notes: list[str]) -> list[dict]:
    res = [x for x in outs if x.result in ("target", "stop")]
    if len(res) < MIN_SAMPLE:
        notes.append(f"מדגם קטן ({len(res)} אותות שהושלמו, נדרשים {MIN_SAMPLE}+) – לא ניתנות הצעות להתאמת משקלים")
        return []
    win = [x for x in res if x.result == "target"]
    lose = [x for x in res if x.result == "stop"]
    if len(win) < 8 or len(lose) < 8:
        notes.append("פחות מ-8 מנצחים או מפסידים – ההבדלים בין הרכיבים אינם אמינים")
        return []
    sugg = []
    for k in WEIGHT_KEYS:
        w = np.mean([x.comps.get(k, 0) for x in win]); l = np.mean([x.comps.get(k, 0) for x in lose])
        d = w - l
        if d >= 0.15:
            sugg.append({"component": k, "direction": "+", "delta": 2, "text": f"{k}: מנצחים {w:.0%} מול מפסידים {l:.0%} מהניקוד המקסימלי → שיקול העלאת משקל ב-2 נקודות (דורש בקטסט)"})
        elif d <= -0.15:
            sugg.append({"component": k, "direction": "-", "delta": 2, "text": f"{k}: מפסידים גבוהים יותר ({l:.0%} מול {w:.0%}) → שיקול הורדת משקל ב-2 נקודות (דורש בקטסט)"})
    notes.append("הצעות מבוססות מתאם בדגימה קטנה ועלולות להיות רעש; אין להפעיל ללא בקטסט walk-forward")
    return sugg


def save_report(rep: WeeklyReport, folder) -> str:
    from pathlib import Path
    p = Path(folder)
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"weekly_{rep.generated[:10]}.md"
    f.write_text(rep.to_markdown(), encoding="utf-8")
    return str(f)


# ------------------------------------------------------------------ versioning / approval gate
class ScoreVersions:
    def __init__(self, store: Store):
        self.store = store

    def propose(self, strategy: str, weights: dict, note: str = "") -> str:
        if set(weights) != set(WEIGHT_KEYS) or abs(sum(weights.values()) - 100) > 1e-9:
            raise ValueError("weights must contain all 7 keys and sum to 100")
        ts = datetime.now(timezone.utc)
        ver = f"{strategy}-proposed-{ts.strftime('%Y%m%d%H%M%S')}"
        with self.store.conn() as c:
            c.execute("INSERT INTO score_versions(version,ts,strategy,weights_json,approved,note) VALUES(?,?,?,?,0,?)",
                      (ver, ts.isoformat(timespec="seconds"), strategy, dumps(weights), note))
        return ver

    def attach_backtest(self, version: str, summary: dict) -> None:
        with self.store.conn() as c:
            c.execute("UPDATE score_versions SET backtest_json=? WHERE version=?", (dumps(summary), version))

    def approve(self, version: str, reviewer: str) -> None:
        if not reviewer.strip():
            raise PermissionError("a named human reviewer is required")
        with self.store.conn() as c:
            r = c.execute("SELECT backtest_json FROM score_versions WHERE version=?", (version,)).fetchone()
            if r is None:
                raise KeyError(version)
            if not r["backtest_json"]:
                raise PermissionError("cannot approve a scoring version without an attached backtest")
            c.execute("UPDATE score_versions SET approved=1, approved_by=?, approved_ts=? WHERE version=?",
                      (reviewer, datetime.now(timezone.utc).isoformat(timespec="seconds"), version))

    def list(self) -> list[dict]:
        with self.store.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM score_versions ORDER BY ts DESC")]

    def approved_weights(self, strategy: str) -> dict | None:
        with self.store.conn() as c:
            r = c.execute("SELECT weights_json FROM score_versions WHERE strategy=? AND approved=1 ORDER BY approved_ts DESC LIMIT 1",
                          (strategy,)).fetchone()
        return json.loads(r["weights_json"]) if r else None


def portfolio_review_section(monitor_report, scan) -> str:
    """Weekly long-term review of current holdings: health flags, P/L and the latest long-term score (if scanned)."""
    from .. import i18n
    L = ["", "## סקירת תיק שבועית (טווח ארוך)", ""]
    if monitor_report is None or not monitor_report.positions:
        return "\n".join(L + ["- אין פוזיציות בתיק.", ""])
    m = monitor_report.metrics
    L.append(f"- הון {m['equity']:,.0f}$ | חשיפה {m['exposure_pct']:.0%} | סיכון פתוח {m['open_risk_pct']:.1%} | ירידה מהשיא {m['drawdown']:.1%}")
    for h in sorted(monitor_report.positions, key=lambda x: x.symbol):
        cand = scan.by_symbol(h.symbol) if scan else None
        lt = (cand.scores.get("longterm") if cand else None) or {}
        score = f"ציון טווח ארוך {lt['total']:.0f}" + (" (לא מאומת)" if not lt.get("validated") else "") if lt else "לא נסרקה/לא זכאית"
        flags = "; ".join(i18n.render(r) for r in h.flags) or "תקין"
        L.append(f"- **{h.symbol}** [{i18n.STRATEGY.get(h.strategy, 'לא מוגדר')}] {h.pnl_pct:+.1%} | {score} | {flags}")
    return "\n".join(L + [""])
