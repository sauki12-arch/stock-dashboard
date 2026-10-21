"""AppCore: wires the five layers together. Used by the GUI, the CLI and the scheduled jobs (same code path)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from . import paths, secrets
from .alerts.manager import AlertManager, Outcome, detect_new_setups
from .alerts.telegram import SendResult, TelegramClient
from .config import Settings, load_settings
from .data import market_calendar as cal
from .data.engine import DataEngine, UpdateReport
from .data.mock import MockProvider
from .data.provider import DataProvider
from .data.store import Store
from .data.universe import Universe, load_universe
from .data.yahoo import YahooProvider
from .discovery.indicators import build_frame
from .discovery.scanner import ScanResult, Scanner, load_scan
from .models import Alert
from .portfolio.broker import PaperBroker
from .portfolio.monitor import MonitorReport, PortfolioMonitor
from .portfolio.risk import Decision, PortfolioState, Proposal, RiskEngine
from .portfolio.store import PortfolioStore
from .review.weekly import ScoreVersions, build_report, portfolio_review_section, save_report
from .scheduler import due_jobs

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    job: str
    ok: bool = True
    messages: list[str] = field(default_factory=list)
    update: UpdateReport | None = None
    scan: ScanResult | None = None
    monitor: MonitorReport | None = None
    outcomes: list[Outcome] = field(default_factory=list)


class AppCore:
    def __init__(self, settings: Settings | None = None, *, provider: DataProvider | None = None,
                 clock: Callable[[], datetime] = cal.now_utc, db_path: str | Path | None = None,
                 universe: Universe | None = None, sleep: Callable[[float], None] | None = None):
        self.settings = settings or load_settings()
        self.clock = clock
        self._sleep = sleep
        self.store = Store(db_path or paths.db_path())
        self.universe = universe or load_universe(self.settings.universe_path or None)
        self.pstore = PortfolioStore(self.store, self.settings.risk.starting_cash)
        self.risk = RiskEngine(self.settings.risk)
        self.broker = PaperBroker(self.pstore, self.risk, self.settings.risk)
        self.monitor_engine = PortfolioMonitor(self.risk, self.settings.risk)
        self.versions = ScoreVersions(self.store)
        self.provider = provider or self._make_provider(self.settings.data.provider)
        self._build_pipeline()
        self.alerts = AlertManager(self.store, self.settings.alerts, self._sender(), clock)

    # ---------------------------------------------------------------- wiring
    def _make_provider(self, name: str) -> DataProvider:
        if name == "yahoo":
            return YahooProvider()
        return MockProvider(now=self.clock, sector_map=self.universe.sector)

    def _build_pipeline(self) -> None:
        kw = {"sleep": self._sleep} if self._sleep else {}
        self.engine = DataEngine(self.provider, self.store, self.settings.data, clock=self.clock, **kw)
        self.scanner = Scanner(self.engine, self.store, self.universe, self.settings)

    def set_provider(self, name: str) -> None:
        self.settings.data.provider = name
        self.provider = self._make_provider(name)
        self._build_pipeline()

    def reload_settings(self, settings: Settings) -> None:
        self.settings = settings.validate()
        self.risk = RiskEngine(settings.risk)
        self.broker = PaperBroker(self.pstore, self.risk, settings.risk)
        self.monitor_engine = PortfolioMonitor(self.risk, settings.risk)
        self._build_pipeline()
        self.alerts = AlertManager(self.store, settings.alerts, self._sender(), self.clock)

    def _sender(self):
        creds = secrets.load_telegram()
        if not creds.configured:
            return None
        client = TelegramClient(creds.token, creds.chat_id)
        return client.send

    def refresh_telegram(self) -> None:
        self.alerts.send = self._sender()

    def telegram_test(self) -> SendResult:
        creds = secrets.load_telegram()
        if not creds.configured:
            return SendResult(False, 0, "Telegram credentials are not configured")
        return TelegramClient(creds.token, creds.chat_id).test()

    # ---------------------------------------------------------------- data / scan
    def fetch_symbols(self) -> list[str]:
        held = [p.symbol for p in self.pstore.positions()]
        return list(dict.fromkeys(self.universe.all_fetch_symbols() + held))

    def update_data(self, progress=None, cancel=None, force_full: bool = False) -> UpdateReport:
        return self.engine.update_daily(self.fetch_symbols(), force_full=force_full, progress=progress, cancel=cancel)

    def scan(self, progress=None, cancel=None) -> ScanResult:
        return self.scanner.run(progress=progress, cancel=cancel)

    def latest_scan(self, offset: int = 0) -> ScanResult | None:
        """Latest stored scan (cached by run id so several screens can ask for it cheaply)."""
        if offset:
            return load_scan(self.store, offset=offset)
        with self.store.conn() as c:
            r = c.execute("SELECT MAX(id) m FROM scan_runs").fetchone()
        rid = r["m"] if r else None
        if rid is None:
            return None
        cached = getattr(self, "_scan_cache", None)
        if cached is None or cached.run_id != rid:
            cached = self._scan_cache = load_scan(self.store, run_id=rid)
        return cached

    # ---------------------------------------------------------------- portfolio
    def last_prices(self, symbols: list[str]) -> dict[str, float]:
        out = {}
        for s in symbols:
            df = self.store.load_bars(s)
            if len(df):
                out[s] = float(df["close"].iloc[-1])
        return out

    def portfolio_state(self) -> PortfolioState:
        pos = self.pstore.positions()
        return PortfolioState(self.pstore.cash, pos, self.last_prices([p.symbol for p in pos]))

    def evaluate_proposal(self, symbol: str, entry: float, stop: float, target: float | None = None,
                          sector: str = "", strategy: str = "", shares_override: int | None = None) -> Decision:
        df, q = self.engine.load(symbol, min_bars=1)
        last = float(df["close"].iloc[-1]) if len(df) else None
        detail = ",".join(q.codes()) if not q.ok else ""
        prop = Proposal(symbol, entry, stop, target, sector or self.universe.sector.get(symbol, ""), strategy,
                        last_price=last, data_ok=q.ok, data_detail=detail)
        return self.risk.evaluate(prop, self.portfolio_state(), shares_override)

    def monitor(self, live_prices: dict[str, float] | None = None, record_equity: bool = True) -> MonitorReport:
        st = self.portfolio_state()
        spy = self.engine.load("SPY")[0]
        frames, quality = {}, {}
        for p in st.positions:
            df, q = self.engine.load(p.symbol)
            quality[p.symbol] = q
            if len(df):
                frames[p.symbol] = build_frame(df.tail(520), spy if len(spy) else None)
        rets = None
        if len(frames) >= 2:
            rets = pd.DataFrame({s: f["close"].pct_change().tail(60) for s, f in frames.items()})
        rep = self.monitor_engine.evaluate(st, frames, quality, self.pstore.peak_equity(), rets, live_prices)
        if record_equity:
            d = cal.last_completed_session(self.clock(), self.settings.data.grace_minutes_after_close).isoformat()
            self.pstore.record_equity(d, st.equity, st.cash)
        return rep

    # ---------------------------------------------------------------- jobs
    def run_job(self, job: str, progress=None, cancel=None) -> JobResult:
        jr = JobResult(job)
        try:
            if job in ("pre_market", "post_close", "weekly_review"):
                jr.update = self.update_data(progress, cancel)
                jr.messages.append(jr.update.summary())
                prev = self.latest_scan()
                jr.scan = self.scan(progress, cancel)
                jr.messages.append(f"scan: {jr.scan.n_eligible}/{jr.scan.n_universe} eligible, regime {jr.scan.regime['regime']}"
                                   + (" [SYNTHETIC DATA]" if jr.scan.synthetic else ""))
                jr.monitor = self.monitor()
                alerts: list[Alert] = list(jr.monitor.alerts)
                if jr.update.provider_down:
                    alerts.append(Alert(f"provider_down:{self.clock().date()}", "warning", "provider", "ספק נתונים לא זמין",
                                        "עדכון הנתונים נכשל – הסריקה מבוססת על נתונים ישנים", ""))
                alerts += detect_new_setups(prev, jr.scan, self.settings.alerts)
                jr.outcomes = self.alerts.process(alerts)
                self.alerts.flush_deferred()
                if job == "weekly_review":
                    rep = build_report(self.store, self.clock())
                    rep.notes.append("סעיף סקירת התיק מצורף בסוף הדוח")
                    path = save_report(rep, paths.app_home() / "reports")
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(portfolio_review_section(jr.monitor, jr.scan))
                    jr.messages.append(f"weekly report: {path}")
            elif job == "intraday":
                held = [p.symbol for p in self.pstore.positions()]
                live = {}
                for s in held:
                    r = self.engine.update_intraday(s, "5m", 1)
                    if not isinstance(r, Exception) and hasattr(r, "empty") and not r.empty:
                        live[s] = float(r["close"].iloc[-1])
                jr.messages.append(f"intraday prices for {len(live)}/{len(held)} positions")
                jr.monitor = self.monitor(live_prices=live, record_equity=False)
                jr.outcomes = self.alerts.process(jr.monitor.alerts)
                self.alerts.flush_deferred()
            else:
                raise ValueError(f"unknown job {job}")
            self.store.set_job_run(job, self.clock().isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001 - jobs report failure, they never crash the host process
            log.exception("job %s failed", job)
            jr.ok = False
            jr.messages.append(f"FAILED: {type(e).__name__}: {e}")
        return jr

    def due_jobs(self) -> list[str]:
        last = {j: (datetime.fromisoformat(v) if (v := self.store.get_job_run(j)) else None)
                for j in ("pre_market", "post_close", "intraday", "weekly_review")}
        return due_jobs(self.clock(), last, self.settings.schedule)
