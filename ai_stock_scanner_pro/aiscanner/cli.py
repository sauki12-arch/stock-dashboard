"""Command line entry points (also the headless path for Windows Task Scheduler)."""
from __future__ import annotations

import argparse
import json
import os
import sys


def _core(args):
    from .config import load_settings
    from .core import AppCore
    s = load_settings()
    base = AppCore(s)
    if args.provider and args.provider != s.data.provider:
        if base.would_purge_on_switch(args.provider):
            raise SystemExit(f"refusing --provider {args.provider}: it would delete the stored {'real' if args.provider == 'mock' else 'demo'} market data. "
                             "Use a separate folder (--home) or the Settings screen / clear-demo command deliberately.")
        base.set_provider(args.provider)
    return base


def _progress(label):
    def cb(done, total, msg):
        if total and (done == total or done % max(1, total // 10) == 0):
            print(f"  {label}: {done}/{total} {msg}", flush=True)
    return cb


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aiscanner", description="AI Stock Scanner Pro – Quant Edition (decision support only)")
    ap.add_argument("--provider", choices=["mock", "yahoo"], help="override data provider for this run")
    ap.add_argument("--home", help="data directory (default %%APPDATA%%/AIStockScannerPro)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="launch the desktop app (default)")
    sub.add_parser("update", help="download/refresh market data")
    sc = sub.add_parser("scan", help="update data, scan the universe and print the top candidates")
    sc.add_argument("--top", type=int, default=10)
    j = sub.add_parser("job", help="run a scheduled job headlessly")
    j.add_argument("name", choices=["pre_market", "post_close", "intraday", "weekly_review"])
    sub.add_parser("diagnostics", help="print diagnostics")
    sub.add_parser("telegram-test", help="send a Telegram test message using the saved credentials")
    imp = sub.add_parser("import-portfolio", help="import holdings from CSV")
    imp.add_argument("file")
    bt = sub.add_parser("backtest", help="walk-forward backtest on stored data")
    bt.add_argument("--strategy", choices=["swing", "longterm"], default="swing")
    bt.add_argument("--symbols", type=int, default=50, help="use the first N universe symbols")
    sub.add_parser("sync-portfolio", help="sync the portfolio from the BARCHART folder now (read-only)")
    sub.add_parser("clear-demo", help="delete synthetic/demo market data, scans, alerts and paper trades")
    sub.add_parser("selftest", help="quick end-to-end check on synthetic data")
    args = ap.parse_args(argv)
    if args.home:
        os.environ["AISP_HOME"] = args.home
    cmd = args.cmd or "gui"
    if cmd == "selftest":                       # never touch the user's real database
        import tempfile
        os.environ["AISP_HOME"] = tempfile.mkdtemp(prefix="aisp_selftest_")

    if cmd == "gui":
        from .ui.main_window import run_gui
        return run_gui(args.provider)
    if cmd == "telegram-test":
        from .core import AppCore
        r = AppCore().telegram_test()
        print("OK – message sent" if r.ok else f"FAILED: {r.error}")
        return 0 if r.ok else 1
    core = _core(args)
    if cmd == "update":
        print(core.update_data(progress=_progress("update")).summary())
    elif cmd == "scan":
        print(core.update_data(progress=_progress("update")).summary())
        res = core.scan(progress=_progress("scan"))
        print(f"regime {res.regime['regime']} score {res.regime['score']} (uncalibrated) | eligible {res.n_eligible}/{res.n_universe}"
              + (" | SYNTHETIC DATA" if res.synthetic else ""))
        for st in ("swing", "longterm"):
            print(f"\nTop {args.top} {st}:")
            for c in res.ranked(st, args.top):
                lv = c.scores[st]["levels"]
                print(f"  {c.symbol:6} score {c.score(st):5.1f}  entry {lv['entry']:.2f} stop {lv['stop']:.2f} target {lv['target']:.2f} rr {lv['rr']:.2f}")
    elif cmd == "job":
        r = core.run_job(args.name, progress=_progress(args.name))
        print("\n".join(r.messages))
        return 0 if r.ok else 1
    elif cmd == "diagnostics":
        for d in core_diag(core):
            print(f"[{d.status.upper():4}] {d.name}: {d.detail}")
    elif cmd == "import-portfolio":
        from .portfolio.importer import load_positions_csv
        res = load_positions_csv(args.file, core.universe.sector)
        for e in res.errors:
            print("ERROR", e)
        for p in res.positions:
            core.broker.add_manual_position(p)
        print(f"imported {len(res.positions)} positions, {len(res.errors)} errors")
        return 0 if not res.errors else 2
    elif cmd == "sync-portfolio":
        r = core.sync_portfolio(force=True)
        print("folder:", core.settings.barchart.folder, "| enabled:", core.settings.barchart.enabled)
        print(r.status, "-", r.message)
        if r.file:
            print("file:", r.file)
        print(f"added {r.added} updated {r.updated} removed {r.removed}")
        for e in r.errors:
            print("ERROR", e)
        return 0 if r.status in ("ok", "unchanged") else 1
    elif cmd == "clear-demo":
        print(core.clear_demo_data())
    elif cmd == "backtest":
        return _backtest(core, args)
    elif cmd == "selftest":
        return _selftest(core)
    return 0


def core_diag(core):
    from .diagnostics import run_diagnostics
    return run_diagnostics(core)


def _backtest(core, args) -> int:
    import datetime as dt
    from .backtest.engine import BTConfig, build_signals
    from .backtest.walkforward import walk_forward
    syms = core.universe.symbols[: args.symbols]
    frames = {s: core.engine.load(s)[0] for s in syms}
    frames = {s: f for s, f in frames.items() if len(f) > 300}
    spy = core.engine.load("SPY")[0]
    secs = {e: core.engine.load(e)[0] for e in set(core.universe.sector.values()) if e}
    if len(spy) < 800 or not frames:
        print("Not enough stored history (need ~3.5+ years of SPY). Run `update` with a long history_days setting first.")
        return 2
    cfg = BTConfig(strategy=args.strategy)
    sig = build_signals(frames, spy, secs, core.universe.sector, core.settings.scan, cfg, progress=_progress("signals"))
    wf = walk_forward(sig, frames, spy, cfg, core.universe.sector)
    st = wf.stats
    print(f"Out-of-sample walk-forward ({len(wf.windows)} windows), {st.n_trades} trades"
          + (" – SYNTHETIC DATA" if core.provider.is_synthetic else ""))
    print(json.dumps({k: v for k, v in st.to_dict().items() if k != "notes"}, indent=2, default=str))
    print("SPY buy&hold over same span:", wf.benchmark)
    for n in st.notes + wf.disclosures:
        print("-", n)
    return 0


def _selftest(core) -> int:
    core.set_provider("mock")
    print(core.update_data().summary())
    res = core.scan()
    top = res.ranked("swing", 3)
    ok = res.n_eligible > 0 and bool(top)
    print("selftest", "PASSED" if ok else "FAILED", f"({res.n_eligible} eligible, top swing: {[c.symbol for c in top]})")
    return 0 if ok else 1
