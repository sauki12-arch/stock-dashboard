import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiscanner.config import Settings
from aiscanner.core import AppCore
from aiscanner.data.mock import MockProvider
from aiscanner.data.store import Store
from aiscanner.data.universe import Universe
from aiscanner.portfolio import barchart_sync as B
from aiscanner.portfolio.store import PortfolioStore, Position

NOW = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)


def ps():
    return PortfolioStore(Store(":memory:"), 50_000)


def write(folder: Path, obj, name="portfolio.json"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(obj), encoding="utf-8")


LIST = [{"symbol": "AAPL", "shares": 10, "entry": 150.5, "stop": 140, "target": 180, "opened": "2026-01-05", "sector": "Technology"},
        {"symbol": "brk.b", "shares": "5", "entry": "400", "stop_loss": 380, "sector": "Financials"}]


def test_parses_list_wrapped_and_keyed_shapes(tmp_path):
    for i, obj in enumerate((LIST, {"positions": LIST, "cash": 12345.6}, {"AAPL": LIST[0], "BRK-B": LIST[1]})):
        p = ps()
        write(tmp_path / str(i), obj)
        r = B.sync_from_folder(p, tmp_path / str(i))
        assert r.status == "ok", r
        got = {x.symbol: x for x in p.positions()}
        assert set(got) == {"AAPL", "BRK-B"}                       # BRK.B normalised for Yahoo
        assert got["AAPL"].qty == 10 and got["AAPL"].avg_cost == 150.5 and got["AAPL"].stop == 140 and got["AAPL"].target == 180
        assert got["AAPL"].sector == "XLK" and got["BRK-B"].sector == "XLF" and got["BRK-B"].stop == 380
        assert got["AAPL"].notes == "barchart"
        if i == 1:
            assert r.cash_set == 12345.6 and p.cash == 12345.6
        else:
            assert p.cash == 50_000


def test_unchanged_then_update_add_remove(tmp_path):
    p = ps()
    write(tmp_path, LIST)
    assert B.sync_from_folder(p, tmp_path).status == "ok"
    assert B.sync_from_folder(p, tmp_path).status == "unchanged"
    changed = [dict(LIST[0], shares=20, stop=145), {"symbol": "MSFT", "shares": 3, "entry": 300}]        # BRK-B dropped
    write(tmp_path, changed)
    r = B.sync_from_folder(p, tmp_path)
    assert r.updated == ["AAPL"] and r.added == ["MSFT"] and r.removed == ["BRK-B"]
    assert p.get("AAPL").qty == 20 and p.get("AAPL").stop == 145 and p.get("BRK-B") is None


def test_manual_and_paper_positions_are_never_removed_or_wiped(tmp_path):
    p = ps()
    p.upsert_position(Position("TSLA", 1, 200.0, notes="paper"))
    p.upsert_position(Position("NVDA", 1, 100.0, notes=""))
    write(tmp_path, [LIST[0]])
    B.sync_from_folder(p, tmp_path)
    assert p.get("TSLA") and p.get("NVDA")
    write(tmp_path, [])                                              # BARCHART portfolio is now empty
    r = B.sync_from_folder(p, tmp_path)
    assert r.removed == ["AAPL"] and p.get("TSLA") and p.get("NVDA")


def test_local_strategy_survives_sync(tmp_path):
    p = ps()
    write(tmp_path, [LIST[0]])
    B.sync_from_folder(p, tmp_path)
    pos = p.get("AAPL"); pos.strategy = "longterm"; p.upsert_position(pos)
    write(tmp_path, [dict(LIST[0], shares=11)])
    B.sync_from_folder(p, tmp_path)
    assert p.get("AAPL").strategy == "longterm" and p.get("AAPL").qty == 11


def test_corrupt_or_half_written_file_changes_nothing(tmp_path):
    p = ps()
    write(tmp_path, LIST)
    B.sync_from_folder(p, tmp_path)
    (tmp_path / "portfolio.json").write_text('[{"symbol": "AAPL", "shares": 10, "ent', encoding="utf-8")      # mid-write
    r = B.sync_from_folder(p, tmp_path)
    assert r.status == "unreadable" and len(p.positions()) == 2
    (tmp_path / "portfolio.json").write_text('{"something": "else"}', encoding="utf-8")
    r = B.sync_from_folder(p, tmp_path)
    assert r.status == "invalid" and len(p.positions()) == 2


def test_bad_rows_reported_and_block_removals(tmp_path):
    p = ps()
    write(tmp_path, LIST)
    B.sync_from_folder(p, tmp_path)
    write(tmp_path, [LIST[0], {"symbol": "BAD", "shares": -1, "entry": 5}, {"symbol": "SHORTY", "shares": 1, "entry": 5, "side": "short"}])
    r = B.sync_from_folder(p, tmp_path)
    assert len(r.errors) == 2 and r.removed == []                  # BRK-B kept: file had unreadable rows, no deletion on partial info
    assert p.get("BRK-B") is not None
    assert p.store.kv_get("barchart_hash") != hashlib.sha256((tmp_path / "portfolio.json").read_bytes()).hexdigest()   # retried next time


def test_closed_positions_skipped(tmp_path):
    p = ps()
    write(tmp_path, [LIST[0], {"symbol": "OLD", "shares": 1, "entry": 1, "status": "closed"}])
    B.sync_from_folder(p, tmp_path)
    assert [x.symbol for x in p.positions()] == ["AAPL"]


def test_missing_folder_and_file(tmp_path):
    p = ps()
    assert B.sync_from_folder(p, tmp_path / "nope").status == "no_folder"
    (tmp_path / "empty").mkdir()
    assert B.sync_from_folder(p, tmp_path / "empty").status == "no_file"


def test_csv_fallback(tmp_path):
    (tmp_path / "portfolio.csv").write_text("ticker,shares,entry,stop_loss\nAAPL,10,150,140\n", encoding="utf-8")
    p = ps()
    r = B.sync_from_folder(p, tmp_path)
    assert r.status == "ok" and p.get("AAPL").stop == 140 and p.get("AAPL").notes == "barchart"


def test_barchart_files_are_never_modified(tmp_path):
    write(tmp_path, LIST)
    (tmp_path / "closed_trades.json").write_text("[]")
    before = {f.name: (f.stat().st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest()) for f in tmp_path.iterdir()}
    p = ps()
    B.sync_from_folder(p, tmp_path)
    B.sync_from_folder(p, tmp_path, force=True)
    after = {f.name: (f.stat().st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest()) for f in tmp_path.iterdir()}
    assert before == after and set(before) == set(after)


# ---------------------------------------------------------------- core integration
def mk_core(tmp_path, synthetic=True, folder=None):
    s = Settings()
    s.barchart.enabled = folder is not None
    s.barchart.folder = str(folder) if folder else s.barchart.folder
    u = Universe(symbols=["AAPL", "MSFT"], sector={"AAPL": "XLK", "MSFT": "XLK"})
    prov = MockProvider(now=lambda: NOW, sector_map=u.sector) if synthetic else None
    return AppCore(s, clock=lambda: NOW, db_path=tmp_path / "c.db", universe=u, sleep=lambda x: None, provider=prov)


def test_core_sync_fetches_held_symbols_and_throttles(tmp_path):
    folder = tmp_path / "BARCHART"
    write(folder, [{"symbol": "XOM", "shares": 5, "entry": 100, "stop": 90}])
    c = mk_core(tmp_path, folder=folder)
    c.update_data()
    assert c.pstore.get("XOM") and "XOM" in c.fetch_symbols() and c.store.last_bar_date("XOM")
    n = c._last_sync_at
    assert c.sync_portfolio(min_interval=True) is c.last_sync and c._last_sync_at == n      # throttled by interval
    assert c.settings.barchart.folder == str(folder)


def test_core_sync_failure_is_alerted_not_raised(tmp_path):
    c = mk_core(tmp_path, folder=tmp_path / "missing")
    r = c.sync_portfolio(force=True)
    assert r.status == "no_folder"
    assert any(a["kind"] == "sync_error" for a in c.alerts.history())


def test_default_settings_use_real_provider_and_barchart_folder():
    s = Settings()
    assert s.data.provider == "yahoo" and s.barchart.enabled and s.barchart.folder == r"C:\Projects\BARCHART"


def test_origin_guard_purges_demo_data_when_switching_to_real(tmp_path):
    c = mk_core(tmp_path)
    c.update_data()
    c.scan()
    c.broker.add_manual_position(Position("KEEP", 1, 10.0))                                 # manual: must survive
    c.pstore.upsert_position(Position("DEMO", 5, 10.0, notes="paper"))
    assert c.store.kv_get("data_origin") == "synthetic" and c.store.bar_counts()
    assert c.would_purge_on_switch("yahoo") and not c.would_purge_on_switch("mock")
    # reopen the same database with a REAL provider (yfinance is faked and offline – nothing is downloaded)
    from aiscanner.data.yahoo import YahooProvider
    c2 = AppCore(Settings(), clock=lambda: NOW, db_path=tmp_path / "c.db", universe=c.universe, sleep=lambda x: None,
                 provider=YahooProvider(downloader=lambda **kw: __import__("pandas").DataFrame()))
    assert c2.store.bar_counts() == {} and c2.latest_scan() is None and c2.alerts.history() == []
    assert c2.pstore.get("DEMO") is None and c2.pstore.get("KEEP") is not None
    assert c2.pstore.cash == c2.settings.risk.starting_cash


def test_legacy_database_without_origin_tag_is_treated_as_demo(tmp_path):
    c = mk_core(tmp_path)
    c.update_data()
    c.store.kv_set("data_origin", "")                                    # simulate an older build
    with c.store.conn() as cn:
        cn.execute("DELETE FROM account WHERE key='data_origin'")
    from aiscanner.data.yahoo import YahooProvider
    c2 = AppCore(Settings(), clock=lambda: NOW, db_path=tmp_path / "c.db", universe=c.universe, sleep=lambda x: None,
                 provider=YahooProvider(downloader=lambda **kw: __import__("pandas").DataFrame()))
    assert c2.store.bar_counts() == {}


def test_clear_demo_data_command(tmp_path):
    c = mk_core(tmp_path)
    c.update_data()
    c.scan()
    c.pstore.upsert_position(Position("DEMO", 5, 10.0, notes="paper"))
    c.broker.add_manual_position(Position("KEEP", 1, 10.0))
    out = c.clear_demo_data()
    assert out["bars"] > 0 and out["scan_runs"] == 1 and out["paper_positions"] == 1
    assert c.store.bar_counts() == {} and c.latest_scan() is None
    assert [p.symbol for p in c.pstore.positions()] == ["KEEP"]


def test_real_data_is_not_purged_when_staying_real(tmp_path):
    c = mk_core(tmp_path)
    c.store.kv_set("data_origin", "real")
    c.store.upsert_bars("SPY", __import__("tests.helpers", fromlist=["x"]).trend(30))
    from aiscanner.data.yahoo import YahooProvider
    c2 = AppCore(Settings(), clock=lambda: NOW, db_path=tmp_path / "c.db", universe=c.universe, sleep=lambda x: None,
                 provider=YahooProvider(downloader=lambda **kw: None))
    assert c2.store.bar_counts().get("SPY") == 30
