"""SQLite persistence (WAL mode). One connection per call-site thread via short-lived connections."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(
  symbol TEXT NOT NULL, interval TEXT NOT NULL, ts TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY(symbol, interval, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fetch_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, interval TEXT, status TEXT, rows INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS symbol_meta(
  symbol TEXT PRIMARY KEY, last_full_refresh TEXT, last_bar TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS scan_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, as_of TEXT, regime TEXT, n_universe INTEGER,
  n_eligible INTEGER, provider TEXT, synthetic INTEGER, version TEXT, payload_json TEXT);
CREATE TABLE IF NOT EXISTS scan_results(
  run_id INTEGER, symbol TEXT, strategy TEXT, score REAL, klass TEXT, validated INTEGER, eligible INTEGER,
  payload_json TEXT, PRIMARY KEY(run_id, symbol, strategy));
CREATE TABLE IF NOT EXISTS positions(
  symbol TEXT PRIMARY KEY, qty REAL NOT NULL, avg_cost REAL NOT NULL, stop REAL, target REAL,
  sector TEXT, strategy TEXT, opened_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
  fees REAL, realized_pl REAL, note TEXT);
CREATE TABLE IF NOT EXISTS account(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS equity_history(date TEXT PRIMARY KEY, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, dedupe_key TEXT, severity TEXT, kind TEXT, symbol TEXT,
  title TEXT, body TEXT, status TEXT, sent_ts TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_alerts_key ON alerts(dedupe_key, ts);
CREATE TABLE IF NOT EXISTS job_runs(job TEXT PRIMARY KEY, last_run_ts TEXT);
CREATE TABLE IF NOT EXISTS score_versions(
  version TEXT PRIMARY KEY, ts TEXT, strategy TEXT, weights_json TEXT, approved INTEGER, note TEXT,
  backtest_json TEXT, approved_by TEXT, approved_ts TEXT);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._mem_conn: sqlite3.Connection | None = None
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        with self._lock:
            if self.path == ":memory:":
                if self._mem_conn is None:
                    self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                    self._mem_conn.row_factory = sqlite3.Row
                c = self._mem_conn
                try:
                    yield c
                    c.commit()
                except Exception:
                    c.rollback()
                    raise
                return
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    # ---- bars --------------------------------------------------------
    @staticmethod
    def _ts(idx: pd.DatetimeIndex, interval: str) -> list[str]:
        if interval == "1d":
            return [d.strftime("%Y-%m-%d") for d in idx]
        return [d.strftime("%Y-%m-%dT%H:%M:%S") for d in idx]

    def upsert_bars(self, symbol: str, df: pd.DataFrame, interval: str = "1d") -> int:
        if df.empty:
            return 0
        rows = list(zip([symbol] * len(df), [interval] * len(df), self._ts(df.index, interval),
                        df["open"], df["high"], df["low"], df["close"], df["volume"]))
        with self.conn() as c:
            c.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def delete_bars(self, symbol: str, interval: str = "1d", before: str | None = None) -> None:
        with self.conn() as c:
            if before:
                c.execute("DELETE FROM bars WHERE symbol=? AND interval=? AND ts<?", (symbol, interval, before))
            else:
                c.execute("DELETE FROM bars WHERE symbol=? AND interval=?", (symbol, interval))

    def load_bars(self, symbol: str, interval: str = "1d", since: str | None = None,
                  until: str | None = None) -> pd.DataFrame:
        q = "SELECT ts,open,high,low,close,volume FROM bars WHERE symbol=? AND interval=?"
        args: list = [symbol, interval]
        if since:
            q += " AND ts>=?"
            args.append(since)
        if until:
            q += " AND ts<=?"
            args.append(until)
        q += " ORDER BY ts"
        with self.conn() as c:
            rows = c.execute(q, args).fetchall()
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                index=pd.DatetimeIndex([], name="date"), dtype="float64")
        df = pd.DataFrame([tuple(r) for r in rows], columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts")), name="date").astype("datetime64[ns]")
        return df.astype("float64")

    def last_bar_date(self, symbol: str, interval: str = "1d") -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT MAX(ts) m FROM bars WHERE symbol=? AND interval=?", (symbol, interval)).fetchone()
        return r["m"] if r else None

    def bar_counts(self) -> dict[str, int]:
        with self.conn() as c:
            return {r["symbol"]: r["n"] for r in c.execute(
                "SELECT symbol, COUNT(*) n FROM bars WHERE interval='1d' GROUP BY symbol")}

    # ---- logs / meta -------------------------------------------------
    def log_fetch(self, symbol: str, interval: str, status: str, rows: int, message: str = "") -> None:
        with self.conn() as c:
            c.execute("INSERT INTO fetch_log(ts,symbol,interval,status,rows,message) VALUES(?,?,?,?,?,?)",
                      (utcnow_iso(), symbol, interval, status, rows, message[:500]))

    def recent_fetch_errors(self, limit: int = 100) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT ts,symbol,interval,status,rows,message FROM fetch_log WHERE status!='ok' "
                "ORDER BY id DESC LIMIT ?", (limit,))]

    def set_meta(self, symbol: str, **kw) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO symbol_meta(symbol) VALUES(?)", (symbol,))
            for k, v in kw.items():
                if k not in ("last_full_refresh", "last_bar", "note"):
                    raise KeyError(k)
                c.execute(f"UPDATE symbol_meta SET {k}=? WHERE symbol=?", (v, symbol))

    def get_meta(self, symbol: str) -> dict:
        with self.conn() as c:
            r = c.execute("SELECT * FROM symbol_meta WHERE symbol=?", (symbol,)).fetchone()
        return dict(r) if r else {}

    # ---- misc key/value ----------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT value FROM account WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def kv_set(self, key: str, value) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO account(key,value) VALUES(?,?)", (key, str(value)))

    def get_job_run(self, job: str) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT last_run_ts FROM job_runs WHERE job=?", (job,)).fetchone()
        return r["last_run_ts"] if r else None

    def set_job_run(self, job: str, ts_iso: str) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO job_runs VALUES(?,?)", (job, ts_iso))

    def table_counts(self) -> dict[str, int]:
        out = {}
        with self.conn() as c:
            for t in ("bars", "scan_runs", "positions", "trades", "alerts", "fetch_log"):
                out[t] = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        return out


def dumps(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
