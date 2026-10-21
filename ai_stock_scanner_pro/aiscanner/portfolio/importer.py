"""CSV import of existing holdings.

Columns (header required, case-insensitive): symbol, quantity, avg_cost  [stop, target, sector, strategy, opened_at, notes]
Aliases: qty/shares → quantity; cost/avg_price/average_cost → avg_cost.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..data.universe import SECTOR_ETFS, SYMBOL_RE
from .store import Position

ALIASES = {"qty": "quantity", "shares": "quantity", "cost": "avg_cost", "avg_price": "avg_cost",
           "average_cost": "avg_cost", "ticker": "symbol", "sector_etf": "sector"}


@dataclass
class ImportResult:
    positions: list[Position] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _num(s: str, name: str, line: int, required: bool = True, positive: bool = True):
    s = (s or "").strip().replace(",", "")
    if not s:
        if required:
            raise ValueError(f"line {line}: missing {name}")
        return None
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"line {line}: {name} is not a number: {s!r}") from None
    if not math.isfinite(v) or (positive and v <= 0):
        raise ValueError(f"line {line}: {name} must be > 0")
    return v


def parse_positions_csv(text: str, sector_lookup: dict[str, str] | None = None) -> ImportResult:
    res = ImportResult()
    rd = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not rd.fieldnames:
        res.errors.append("empty file")
        return res
    names = {h: ALIASES.get(h.strip().lower(), h.strip().lower()) for h in rd.fieldnames}
    missing = {"symbol", "quantity", "avg_cost"} - set(names.values())
    if missing:
        res.errors.append(f"missing columns: {sorted(missing)}")
        return res
    seen: set[str] = set()
    for n, raw in enumerate(rd, start=2):
        row = {names[k]: (v or "").strip() for k, v in raw.items() if k in names}
        try:
            sym = row["symbol"].upper()
            if not SYMBOL_RE.match(sym):
                raise ValueError(f"line {n}: invalid symbol {sym!r}")
            if sym in seen:
                raise ValueError(f"line {n}: duplicate symbol {sym}")
            qty = _num(row["quantity"], "quantity", n)
            cost = _num(row["avg_cost"], "avg_cost", n)
            stop = _num(row.get("stop", ""), "stop", n, required=False)
            target = _num(row.get("target", ""), "target", n, required=False)
            if stop is not None and stop >= cost * 5:
                raise ValueError(f"line {n}: stop looks wrong")
            sector = row.get("sector", "").upper() or (sector_lookup or {}).get(sym, "")
            if sector and sector not in SECTOR_ETFS:
                raise ValueError(f"line {n}: unknown sector {sector!r} (use a sector ETF like XLK)")
            strat = row.get("strategy", "").lower()
            if strat not in ("", "swing", "longterm"):
                raise ValueError(f"line {n}: strategy must be swing or longterm")
        except ValueError as e:
            res.errors.append(str(e))
            continue
        seen.add(sym)
        res.positions.append(Position(sym, qty, cost, stop, target, sector, strat, row.get("opened_at", ""), row.get("notes", "")))
    return res


def load_positions_csv(path: str | Path, sector_lookup: dict[str, str] | None = None) -> ImportResult:
    return parse_positions_csv(Path(path).read_text(encoding="utf-8-sig"), sector_lookup)
