"""Configurable universe: CSV with columns `symbol,sector_etf` (sector_etf optional)."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^]{0,9}$")
SECTOR_ETFS = {"XLK": "טכנולוגיה", "XLF": "פיננסים", "XLV": "בריאות", "XLE": "אנרגיה",
               "XLY": "צריכה מחזורית", "XLP": "צריכה בסיסית", "XLI": "תעשייה", "XLB": "חומרי גלם",
               "XLU": "שירותים", "XLRE": "נדל\"ן", "XLC": "תקשורת"}
BENCHMARKS = ["SPY", "QQQ", "^VIX"]


@dataclass
class Universe:
    symbols: list[str] = field(default_factory=list)
    sector: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.symbols)

    def support_symbols(self) -> list[str]:
        """Benchmarks and sector ETFs that must be fetched alongside the stocks."""
        return BENCHMARKS + list(SECTOR_ETFS)

    def all_fetch_symbols(self) -> list[str]:
        seen, out = set(), []
        for s in self.symbols + self.support_symbols():
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out


def load_universe(path: str | Path | None = None) -> Universe:
    p = Path(path) if path else paths.default_universe_path()
    u = Universe()
    seen: set[str] = set()
    with open(p, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames or "symbol" not in [h.strip().lower() for h in rd.fieldnames]:
            raise ValueError(f"{p}: CSV must have a 'symbol' column")
        for n, row in enumerate(rd, start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            sym = row["symbol"].upper()
            if not sym:
                continue
            if not SYMBOL_RE.match(sym):
                u.errors.append(f"line {n}: invalid symbol {sym!r}")
                continue
            if sym in seen:
                u.errors.append(f"line {n}: duplicate {sym}")
                continue
            seen.add(sym)
            u.symbols.append(sym)
            etf = row.get("sector_etf", "").upper()
            if etf and etf not in SECTOR_ETFS:
                u.errors.append(f"line {n}: {sym}: unknown sector ETF {etf!r}")
                etf = ""
            u.sector[sym] = etf
    return u
