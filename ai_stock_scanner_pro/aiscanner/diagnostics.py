"""Environment/self diagnostics shown in Settings → Diagnostics and via `python -m aiscanner diagnostics`."""
from __future__ import annotations

import importlib
import platform
import sys
from dataclasses import dataclass

from . import __version__, paths, secrets
from .data import market_calendar as cal
from .scheduler import next_runs

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class DiagItem:
    name: str
    status: str
    detail: str


def _ver(mod: str) -> str:
    try:
        m = importlib.import_module(mod)
        return getattr(m, "__version__", "installed")
    except Exception as e:  # noqa: BLE001
        return f"MISSING ({type(e).__name__})"


def run_diagnostics(core, check_provider: bool = False) -> list[DiagItem]:
    out: list[DiagItem] = []
    add = lambda n, s, d: out.append(DiagItem(n, s, d))                                  # noqa: E731
    add("version", OK, f"AI Stock Scanner Pro {__version__} | Python {platform.python_version()} | {platform.platform()}")
    for mod in ("pandas", "numpy", "PySide6", "pyqtgraph", "yfinance", "requests", "yaml"):
        v = _ver(mod)
        add(f"lib:{mod}", FAIL if v.startswith("MISSING") and mod != "yfinance" else WARN if v.startswith("MISSING") else OK, v)
    add("frozen_exe", OK, "yes (PyInstaller)" if getattr(sys, "frozen", False) else "no (running from source)")
    add("home", OK, str(paths.app_home()))
    try:
        counts = core.store.table_counts()
        add("database", OK, ", ".join(f"{k}={v}" for k, v in counts.items()))
    except Exception as e:  # noqa: BLE001
        add("database", FAIL, str(e))
    u = core.universe
    add("universe", OK if len(u) >= 400 else WARN, f"{len(u)} symbols; {len(u.errors)} CSV issues")
    p = core.provider
    add("provider", WARN if p.is_synthetic else OK, p.name + (" – SYNTHETIC DATA, not for decisions" if p.is_synthetic else ""))
    if check_provider:
        ok, msg = p.health_check()
        add("provider_health", OK if ok else FAIL, msg)
    now = core.clock()
    add("market_state", OK, f"{cal.market_state(now)} | NY time {cal.to_ny(now):%Y-%m-%d %H:%M} | last completed session {cal.last_completed_session(now, 20)}")
    counts = core.store.bar_counts()
    if counts:
        spy_last = core.store.last_bar_date("SPY")
        stale = cal.sessions_behind(__import__("pandas").Timestamp(spy_last).date(), now, 20) if spy_last else None
        add("data_freshness", OK if stale == 0 else WARN, f"SPY last bar {spy_last}; {stale} sessions behind; {len(counts)} symbols stored")
    else:
        add("data_freshness", WARN, "no market data stored yet – run an update")
    errs = core.store.recent_fetch_errors(1000)
    add("fetch_errors", OK if not errs else WARN, f"{len(errs)} logged fetch errors" + (f"; latest: {errs[0]['symbol']}: {errs[0]['message']}" if errs else ""))
    sc = core.latest_scan()
    if sc:
        add("last_scan", WARN if sc.synthetic else OK, f"{sc.ts} as_of {sc.as_of}, {sc.n_eligible}/{sc.n_universe} eligible, regime {sc.regime['regime']}"
            + (", SYNTHETIC" if sc.synthetic else "") + f", {len(sc.failed)} failed symbols")
    else:
        add("last_scan", WARN, "no scan has been run yet")
    creds = secrets.load_telegram()
    add("telegram", OK if creds.configured else WARN,
        "configured" + (" (encrypted with Windows DPAPI)" if creds.protected else " (NOT encrypted – non-Windows/dev)") if creds.configured
        else "not configured – alerts are stored in history only")
    add("scheduler", OK if core.settings.schedule.enabled else WARN,
        ("enabled; next: " + "; ".join(f"{k}={v:%a %d/%m %H:%M} ET" for k, v in next_runs(now, core.settings.schedule).items() if v))
        if core.settings.schedule.enabled else "disabled (enable in Settings; the app must be running)")
    add("execution_mode", OK, "ANALYSIS + PAPER TRADING ONLY – no brokerage connectivity exists")
    return out
