"""One-way, READ-ONLY sync of the BARCHART project's portfolio into this app.

BARCHART's files (portfolio.json, closed_trades.json, apex_settings.json ...) are only ever opened for reading – the bridge is
deliberately one-directional so the two programs never fight over one state file. Positions that came from BARCHART are tagged
`notes="barchart"`; manual and paper positions are never deleted by a sync.

The BARCHART file format is not guaranteed, so the reader is tolerant: a list of position dicts, a dict wrapping such a list
(`positions` / `holdings` / ...), or a dict keyed by symbol; field names are matched by alias
(symbol|ticker, shares|qty|quantity, entry|buy_price|avg_cost, stop|stop_loss, target|take_profit, opened|buy_date).
Anything it cannot understand is reported as an error – never guessed. A half-written / corrupt file changes nothing.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..data.universe import SECTOR_ETFS, SYMBOL_RE
from .importer import parse_positions_csv
from .store import Position, PortfolioStore

SOURCE_TAG = "barchart"
CANDIDATE_FILES = ["portfolio.json", "positions.json", "portfolio.csv", "positions.csv"]
LIST_KEYS = ("positions", "holdings", "open_positions", "portfolio", "trades", "items")
CASH_KEYS = ("cash", "cash_balance", "free_cash", "available_cash")

F_SYMBOL = ("symbol", "ticker", "sym")
F_QTY = ("shares", "qty", "quantity", "size")
F_ENTRY = ("entry", "buy_price", "avg_cost", "avg_price", "entry_price", "cost", "price")
F_STOP = ("stop", "stop_loss", "stoploss")
F_TARGET = ("target", "take_profit", "tp", "target_price")
F_OPENED = ("opened", "buy_date", "opened_at", "entry_date", "date")

GICS_TO_ETF = {"technology": "XLK", "information technology": "XLK", "tech": "XLK", "financials": "XLF", "financial": "XLF",
               "financial services": "XLF", "health care": "XLV", "healthcare": "XLV", "energy": "XLE",
               "consumer discretionary": "XLY", "consumer cyclical": "XLY", "consumer staples": "XLP",
               "consumer defensive": "XLP", "industrials": "XLI", "materials": "XLB", "basic materials": "XLB",
               "utilities": "XLU", "real estate": "XLRE", "communication services": "XLC", "communications": "XLC"}
HE_TO_ETF = {v: k for k, v in SECTOR_ETFS.items()}


@dataclass
class SyncResult:
    status: str                       # ok | unchanged | no_folder | no_file | unreadable | invalid | disabled
    file: str = ""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cash_set: float | None = None
    message: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed or self.cash_set is not None)


def find_file(folder: str | Path) -> Path | None:
    """Exact well-known names first (also in data/ subfolders), then any json/csv whose name looks like a portfolio."""
    f = Path(folder)
    if not f.is_dir():
        return None
    for base in (f, f / "data", f / "Data"):
        for name in CANDIDATE_FILES:
            if (base / name).is_file():
                return base / name
    hints = ("portfolio", "position", "holding")
    found = [x for base in (f, f / "data", f / "Data") if base.is_dir() for x in base.iterdir()
             if x.is_file() and x.suffix.lower() in (".json", ".csv") and any(h in x.name.lower() for h in hints)
             and "closed" not in x.name.lower() and "history" not in x.name.lower()]
    return max(found, key=lambda x: x.stat().st_mtime) if found else None


def describe(obj, depth: int = 0) -> str:
    """Short structural summary used in error messages (key names only – never values)."""
    if isinstance(obj, list):
        return f"list[{len(obj)}] of " + (describe(obj[0], depth + 1) if obj else "nothing")
    if isinstance(obj, dict):
        keys = list(obj)[:12]
        inner = ""
        if depth < 2 and obj:
            first = next(iter(obj.values()))
            if isinstance(first, (dict, list)):
                inner = " -> first value: " + describe(first, depth + 1)
        return "dict{" + ", ".join(map(str, keys)) + ("…" if len(obj) > 12 else "") + "}" + inner
    return type(obj).__name__


def _pick(d: dict, keys) -> object:
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def _num(v, name: str, row: str, positive: bool = True) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        raise ValueError(f"{row}: {name} is not a number: {v!r}") from None
    if not math.isfinite(x) or (positive and x <= 0):
        raise ValueError(f"{row}: {name} must be > 0")
    return x


def _sector(raw, symbol: str, lookup: dict[str, str]) -> str:
    if raw:
        r = str(raw).strip()
        if r.upper() in SECTOR_ETFS:
            return r.upper()
        if r.lower() in GICS_TO_ETF:
            return GICS_TO_ETF[r.lower()]
        for he, etf in HE_TO_ETF.items():
            if he in r:
                return etf
    return lookup.get(symbol, "")


def _records(obj) -> tuple[list[tuple[str, dict]], str]:
    """Return [(label, record_dict)] and an optional root-level problem."""
    if isinstance(obj, list):
        return [(f"item {i}", r) for i, r in enumerate(obj) if isinstance(r, dict)], ""
    if isinstance(obj, dict):
        for k in LIST_KEYS:
            v = obj.get(k)
            if isinstance(v, list):
                return [(f"{k}[{i}]", r) for i, r in enumerate(v) if isinstance(r, dict)], ""
            if isinstance(v, dict) and v and all(isinstance(x, dict) for x in v.values()):
                obj = v
                break
        def looks_like_position(k, r) -> bool:
            return bool(SYMBOL_RE.match(str(k).upper().replace(".", "-"))) and _pick(r, F_QTY) is not None
        if obj and all(isinstance(x, dict) for x in obj.values()) and all(looks_like_position(k, r) for k, r in obj.items()):
            return [(str(k), {"symbol": k, **r}) for k, r in obj.items()], ""      # keyed by symbol
    return [], "unrecognised portfolio structure"


out_info: list[str] = []          # informational notes from the last parse (not errors)


def merge_lots(lots: list[Position]) -> Position:
    """Several BARCHART rows for one symbol = several lots: sum shares, weighted-average cost, TIGHTEST (highest) stop so a breach of
    any lot's stop is reported, highest target, earliest open date."""
    qty = sum(x.qty for x in lots)
    cost = sum(x.qty * x.avg_cost for x in lots) / qty
    stops = [x.stop for x in lots if x.stop]
    targets = [x.target for x in lots if x.target]
    opened = sorted(x.opened_at for x in lots if x.opened_at)
    first = lots[0]
    return Position(first.symbol, qty, cost, max(stops) if stops else None, max(targets) if targets else None,
                    first.sector or next((x.sector for x in lots if x.sector), ""), first.strategy, opened[0] if opened else "", SOURCE_TAG)


def parse_barchart_json(obj, sector_lookup: dict[str, str] | None = None) -> tuple[list[Position], list[str], float | None, bool]:
    """-> (positions, errors, cash, recognised). `recognised` is False when the structure itself could not be understood."""
    recs, problem = _records(obj)
    cash = None
    if isinstance(obj, dict):
        c = _pick(obj, CASH_KEYS)
        if c is not None:
            try:
                cash = _num(c, "cash", "root", positive=False)
            except ValueError:
                cash = None
    if problem:
        return [], [problem], cash, False
    lots: dict[str, list[Position]] = {}
    errs: list[str] = []
    for label, r in recs:
        try:
            status = str(_pick(r, ("status", "state")) or "").lower()
            if status in ("closed", "sold", "exited"):
                continue
            side = str(_pick(r, ("side", "direction")) or "long").lower()
            if side in ("short", "sell"):
                raise ValueError(f"{label}: short positions are not supported")
            sym = str(_pick(r, F_SYMBOL) or "").strip().upper().replace(".", "-")
            if not SYMBOL_RE.match(sym):
                raise ValueError(f"{label}: invalid symbol {sym!r}")
            qty = _num(_pick(r, F_QTY), "shares", label)
            cost = _num(_pick(r, F_ENTRY), "entry", label)
            if qty is None or cost is None:
                raise ValueError(f"{label}: missing shares/entry for {sym}")
            stop = _num(_pick(r, F_STOP), "stop", label)
            target = _num(_pick(r, F_TARGET), "target", label)
        except ValueError as e:
            errs.append(str(e))
            continue
        opened = str(_pick(r, F_OPENED) or "")
        lots.setdefault(sym, []).append(Position(sym, qty, cost, stop, target, _sector(_pick(r, ("sector",)), sym, sector_lookup or {}),
                            str(_pick(r, ("strategy",)) or "").lower() if str(_pick(r, ("strategy",)) or "").lower() in ("swing", "longterm") else "",
                            opened, SOURCE_TAG))
    out = [x[0] if len(x) == 1 else merge_lots(x) for x in lots.values()]
    merged = {k: len(v) for k, v in lots.items() if len(v) > 1}
    if merged:
        errs_info = ", ".join(f"{k} ({n} lots)" for k, n in merged.items())
        out_info.append(f"merged lots: {errs_info}")
    return out, errs, cash, True


def sync_from_folder(ps: PortfolioStore, folder: str | Path, sector_lookup: dict[str, str] | None = None,
                     mirror_removals: bool = True, force: bool = False) -> SyncResult:
    f = Path(folder)
    if not f.exists():
        return SyncResult("no_folder", message=f"folder not found: {folder}")
    path = find_file(f)
    if path is None:
        names = sorted(x.name for x in f.iterdir())[:40]
        return SyncResult("no_file", message=f"no portfolio file found in {folder} (looked for {', '.join(CANDIDATE_FILES)} or any json/csv named like portfolio/position/holding). Folder contains: {', '.join(names) or '(empty)'}")
    try:
        raw = path.read_bytes()                                   # read-only open; the file is never modified
    except OSError as e:
        return SyncResult("unreadable", str(path), message=f"cannot read file: {e}")
    digest = hashlib.sha256(raw).hexdigest()
    if not force and ps.store.kv_get("barchart_hash") == digest:
        return SyncResult("unchanged", str(path))
    out_info.clear()
    try:
        text = raw.decode("utf-8-sig")
        if path.suffix.lower() == ".json":
            positions, errs, cash, recognised = parse_barchart_json(json.loads(text), sector_lookup)
        else:
            res = parse_positions_csv(text, sector_lookup)
            positions, errs, cash, recognised = res.positions, res.errors, None, not any("missing columns" in e or e == "empty file" for e in res.errors)
            for p in positions:
                p.notes = SOURCE_TAG
    except (ValueError, UnicodeDecodeError) as e:                  # JSONDecodeError is a ValueError: file mid-write or corrupt
        return SyncResult("unreadable", str(path), message=f"file is not valid yet ({e}); nothing changed – will retry")
    if not recognised:
        try:
            shape = describe(json.loads(text)) if path.suffix.lower() == ".json" else "; ".join(errs[:2])
        except ValueError:
            shape = "?"
        return SyncResult("invalid", str(path), errors=errs, message=f"structure not recognised in {path.name}: {shape}")

    if not positions and errs:
        return SyncResult("invalid", str(path), errors=errs, message=f"no usable positions in {path.name}; first problem: {errs[0]}")
    existing = {p.symbol: p for p in ps.positions()}
    res = SyncResult("ok", str(path), errors=errs)
    new_syms = set()
    for p in positions:
        new_syms.add(p.symbol)
        old = existing.get(p.symbol)
        if old is None:
            res.added.append(p.symbol)
        else:
            same = (abs(old.qty - p.qty) < 1e-9 and abs(old.avg_cost - p.avg_cost) < 1e-9 and old.stop == p.stop
                    and old.target == p.target and old.notes == SOURCE_TAG)
            if same:
                continue
            res.updated.append(p.symbol)
            p.strategy = p.strategy or old.strategy                 # keep a locally chosen strategy BARCHART does not provide
            p.sector = p.sector or old.sector
            p.opened_at = p.opened_at or old.opened_at
        ps.upsert_position(p)
    if mirror_removals:
        for sym, old in existing.items():
            if old.notes == SOURCE_TAG and sym not in new_syms:
                if errs:                                           # some rows were unreadable: do not delete on partial information
                    continue
                ps.delete_position(sym)
                res.removed.append(sym)
    if cash is not None:
        ps.set_cash(cash)
        res.cash_set = cash
    if not errs:
        ps.store.kv_set("barchart_hash", digest)                   # re-process next time if any row failed to parse
    res.message = f"{len(positions)} positions read from {path.name}" + ("; " + "; ".join(out_info) if out_info else "") + (f"; {len(errs)} rows skipped" if errs else "")
    return res
