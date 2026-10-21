"""Alert pipeline: history → dedupe → critical-only Telegram → quiet hours → rate limit → retry/flush."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .. import i18n
from ..config import AlertCfg, parse_hhmm
from ..data import market_calendar as cal
from ..data.store import Store
from ..models import CRITICAL, INFO, LONGTERM, RISK_OFF, STRATEGIES, WARNING, Alert, R
from .telegram import SendResult

SENT, FAILED, REC, DEF_Q, DEF_R, NO_TG, EXPIRED = ("sent", "failed", "recorded", "deferred_quiet", "deferred_rate",
                                                   "not_sent_no_telegram", "expired")
RETRYABLE = (DEF_Q, DEF_R, FAILED)


def in_quiet_hours(now: datetime, start: str, end: str, tz: str) -> bool:
    """True if local time falls in [start, end); the window may wrap midnight. start == end disables quiet hours."""
    (sh, sm), (eh, em) = parse_hhmm(start), parse_hhmm(end)
    t = now.astimezone(ZoneInfo(tz)).time()
    s, e = dtime(sh, sm), dtime(eh, em)
    if s == e:
        return False
    return (s <= t < e) if s < e else (t >= s or t < e)


@dataclass
class Outcome:
    alert: Alert
    status: str
    detail: str = ""


class AlertManager:
    def __init__(self, store: Store, cfg: AlertCfg, send: Callable[[str], SendResult] | None,
                 clock: Callable[[], datetime] = cal.now_utc):
        self.store, self.cfg, self.send, self.clock = store, cfg, send, clock

    # ---------------------------------------------------------------- helpers
    def _iso(self, dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _is_dup(self, key: str, now: datetime) -> bool:
        since = self._iso(now - timedelta(hours=self.cfg.dedupe_hours))
        with self.store.conn() as c:
            r = c.execute("SELECT 1 FROM alerts WHERE dedupe_key=? AND ts>=? LIMIT 1", (key, since)).fetchone()
        return r is not None

    def _sent_last_hour(self, now: datetime) -> int:
        since = self._iso(now - timedelta(hours=1))
        with self.store.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM alerts WHERE status='sent' AND sent_ts>=?", (since,)).fetchone()["n"]

    def format_message(self, a: Alert, now: datetime) -> str:
        local = now.astimezone(ZoneInfo(self.cfg.quiet_tz)).strftime("%d/%m %H:%M")
        return f"🔴 {a.title}\n{a.body}\n\n⏱ {local} | AI Stock Scanner Pro – תמיכת החלטה בלבד, לא הוראת מסחר"

    def _insert(self, a: Alert, now: datetime, status: str, reason: str = "", sent: bool = False) -> int:
        with self.store.conn() as c:
            cur = c.execute("INSERT INTO alerts(ts,dedupe_key,severity,kind,symbol,title,body,status,sent_ts,reason) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (self._iso(now), a.key, a.severity, a.kind, a.symbol, a.title, a.body, status,
                             self._iso(now) if sent else None, reason))
            return cur.lastrowid

    def _set(self, rid: int, status: str, reason: str = "", sent_at: datetime | None = None) -> None:
        with self.store.conn() as c:
            c.execute("UPDATE alerts SET status=?, reason=?, sent_ts=COALESCE(?, sent_ts) WHERE id=?",
                      (status, reason, self._iso(sent_at) if sent_at else None, rid))

    def _try_send(self, a: Alert, now: datetime) -> tuple[str, str]:
        """Decide + attempt delivery for one critical alert. Returns (status, detail)."""
        if not self.cfg.enabled or self.send is None:
            return NO_TG, "telegram disabled or not configured"
        if in_quiet_hours(now, self.cfg.quiet_start, self.cfg.quiet_end, self.cfg.quiet_tz):
            return DEF_Q, "quiet hours"
        if self._sent_last_hour(now) >= self.cfg.max_per_hour:
            return DEF_R, "rate limit"
        res = self.send(self.format_message(a, now))
        return (SENT, "") if res.ok else (FAILED, res.error or f"HTTP {res.status}")

    # ---------------------------------------------------------------- API
    def process(self, alerts: list[Alert]) -> list[Outcome]:
        now = self.clock()
        out: list[Outcome] = []
        seen: set[str] = set()
        for a in alerts:
            if a.key in seen or self._is_dup(a.key, now):
                out.append(Outcome(a, "duplicate_suppressed"))
                continue
            seen.add(a.key)
            if a.severity != CRITICAL:
                self._insert(a, now, REC)
                out.append(Outcome(a, REC))
                continue
            status, detail = self._try_send(a, now)
            self._insert(a, now, status, detail, sent=status == SENT)
            out.append(Outcome(a, status, detail))
        return out

    def flush_deferred(self, max_age_hours: int = 24) -> list[Outcome]:
        """Re-attempt deferred/failed critical alerts, oldest first. Anything older than max_age_hours expires."""
        now = self.clock()
        with self.store.conn() as c:
            rows = c.execute("SELECT * FROM alerts WHERE status IN (?,?,?) ORDER BY id", RETRYABLE).fetchall()
        out = []
        for r in rows:
            a = Alert(r["dedupe_key"], r["severity"], r["kind"], r["title"], r["body"], r["symbol"] or "")
            age = now - datetime.fromisoformat(r["ts"])
            if age > timedelta(hours=max_age_hours):
                self._set(r["id"], EXPIRED, "too old to send")
                out.append(Outcome(a, EXPIRED))
                continue
            status, detail = self._try_send(a, now)
            self._set(r["id"], status, detail, sent_at=now if status == SENT else None)
            out.append(Outcome(a, status, detail))
            if status in (DEF_R, DEF_Q):
                break
        return out

    def history(self, limit: int = 500) -> list[dict]:
        with self.store.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]


def detect_new_setups(prev, cur, cfg: AlertCfg, max_per_strategy: int = 3) -> list[Alert]:
    """Critical alerts for high-score, validated setups that were not already qualifying in the previous scan.
    On synthetic data they are downgraded to INFO (history only – never pushed)."""
    if cur is None or cur.regime.get("regime") == RISK_OFF:
        return []
    out: list[Alert] = []
    iso = datetime.fromisoformat(cur.as_of).isocalendar()
    for st in STRATEGIES:
        prev_ok = set()
        if prev is not None:
            prev_ok = {c.symbol for c in prev.ranked(st) if c.score(st) >= cfg.new_setup_min_score}
        fresh = [c for c in cur.ranked(st) if c.score(st) >= cfg.new_setup_min_score and c.symbol not in prev_ok]
        for c in fresh[:max_per_strategy]:
            lv = c.scores[st].get("levels") or {}
            reason = R("al_new_setup", True, symbol=c.symbol, strategy=i18n.STRATEGY[st], score=c.score(st))
            body = i18n.render(reason)
            if lv:
                body += f"\nכניסה ~{lv['entry']:.2f} | סטופ {lv['stop']:.2f} | יעד {lv['target']:.2f} | R/R {lv['rr']:.2f}"
            body += f"\nמשטר שוק: {i18n.REGIME.get(cur.regime.get('regime'), '')} (לא מכויל)"
            sev = INFO if cur.synthetic else CRITICAL
            title = ("[סינתטי] " if cur.synthetic else "") + f"מועמד חדש: {c.symbol}"
            out.append(Alert(f"new_setup:{c.symbol}:{st}:{iso[0]}-{iso[1]}", sev, "new_setup", title, body, c.symbol))
    return out
