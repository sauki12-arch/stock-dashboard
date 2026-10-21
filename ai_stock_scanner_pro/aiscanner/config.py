"""Typed settings with YAML persistence and validation."""
from __future__ import annotations

import dataclasses as dc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths

WEIGHT_KEYS = ("trend", "momentum", "volume", "setup", "relative_strength", "volatility", "risk_reward")
DEFAULT_WEIGHTS = {"trend": 20, "momentum": 15, "volume": 15, "setup": 15,
                   "relative_strength": 10, "volatility": 10, "risk_reward": 15}


class ConfigError(ValueError):
    pass


@dataclass
class DataCfg:
    provider: str = "mock"            # "mock" | "yahoo"  (mock is the safe default)
    history_days: int = 800           # calendar days of daily history kept
    batch_size: int = 50
    max_workers: int = 3
    requests_per_second: float = 2.0
    max_retries: int = 4
    backoff_base_s: float = 1.5
    overlap_days: int = 10            # re-fetched tail to detect corporate-action revisions
    adjust_tolerance: float = 0.005   # >0.5 % change in overlapping adj close => full refresh
    max_stale_sessions: int = 1
    grace_minutes_after_close: int = 20


@dataclass
class ScanCfg:
    min_price: float = 5.0
    min_dollar_volume: float = 20_000_000.0
    min_bars: int = 220
    max_spread_proxy: float = 0.01    # Corwin-Schultz estimate (fraction of price)
    band_high: int = 85
    band_strong: int = 70
    band_monitor: int = 55
    bands_calibrated: bool = False    # stays False until a calibration record is approved
    swing_weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    longterm_weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    swing_min_rr: float = 1.5
    longterm_min_rr: float = 1.5
    longterm_min_dollar_volume: float = 40_000_000.0
    enable_experimental_patterns: bool = True


@dataclass
class RiskCfg:
    starting_cash: float = 100_000.0
    risk_per_trade: float = 0.01
    max_positions: int = 10
    max_position_pct: float = 0.20
    max_sector_pct: float = 0.35
    max_total_risk_pct: float = 0.08
    slippage_bps: float = 5.0
    fee_per_trade: float = 1.0
    gap_stress_multiple: float = 2.0  # worst-case loss (gap+costs) may not exceed N x risk budget
    max_entry_deviation_pct: float = 0.05
    drawdown_alert_pct: float = 0.10
    correlation_alert: float = 0.85


@dataclass
class AlertCfg:
    enabled: bool = True
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"
    quiet_tz: str = "Asia/Jerusalem"
    max_per_hour: int = 6
    dedupe_hours: int = 24
    new_setup_min_score: int = 85


@dataclass
class ScheduleCfg:
    enabled: bool = False
    pre_market_minutes_before_open: int = 60
    post_close_minutes_after_close: int = 30
    intraday_enabled: bool = False
    intraday_minutes: int = 30
    weekly_review_weekday: int = 5     # Monday=0 ... Saturday=5
    weekly_review_time: str = "09:00"  # America/New_York


@dataclass
class Settings:
    data: DataCfg = field(default_factory=DataCfg)
    scan: ScanCfg = field(default_factory=ScanCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    alerts: AlertCfg = field(default_factory=AlertCfg)
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    universe_path: str = ""           # empty => bundled default

    # ---- validation -------------------------------------------------
    def validate(self) -> "Settings":
        r, d, s, a = self.risk, self.data, self.scan, self.alerts
        _req(0 < r.risk_per_trade <= 0.05, "risk_per_trade must be in (0, 5%]")
        _req(1 <= r.max_positions <= 100, "max_positions must be 1..100")
        _req(0 < r.max_position_pct <= 1, "max_position_pct must be in (0,1]")
        _req(0 < r.max_sector_pct <= 1, "max_sector_pct must be in (0,1]")
        _req(r.max_total_risk_pct >= r.risk_per_trade, "max_total_risk_pct < risk_per_trade")
        _req(r.starting_cash >= 0, "starting_cash must be >= 0")
        _req(d.provider in ("mock", "yahoo"), "provider must be 'mock' or 'yahoo'")
        _req(d.batch_size >= 1 and d.max_workers >= 1, "batch_size/max_workers must be >= 1")
        _req(d.requests_per_second > 0, "requests_per_second must be > 0")
        _req(s.band_high > s.band_strong > s.band_monitor > 0, "score bands must be descending")
        for name in ("swing_weights", "longterm_weights"):
            w = getattr(s, name)
            _req(set(w) == set(WEIGHT_KEYS), f"{name}: keys must be {WEIGHT_KEYS}")
            _req(abs(sum(w.values()) - 100) < 1e-9, f"{name}: weights must sum to 100")
            _req(all(v >= 0 for v in w.values()), f"{name}: weights must be >= 0")
        for t in (a.quiet_start, a.quiet_end, self.schedule.weekly_review_time):
            _parse_hhmm(t)
        _req(a.max_per_hour >= 1, "max_per_hour must be >= 1")
        return self


def _req(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        return h, m
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"invalid HH:MM time: {s!r}") from e


parse_hhmm = _parse_hhmm


def _merge(obj: Any, data: dict) -> None:
    """Recursively copy known keys from `data` onto dataclass `obj`; unknown keys are an error."""
    names = {f.name for f in dc.fields(obj)}
    for k, v in data.items():
        if k not in names:
            raise ConfigError(f"unknown config key: {k!r}")
        cur = getattr(obj, k)
        if dc.is_dataclass(cur):
            if not isinstance(v, dict):
                raise ConfigError(f"{k}: expected a mapping")
            _merge(cur, v)
        elif isinstance(cur, dict):
            setattr(obj, k, dict(v))
        else:
            setattr(obj, k, type(cur)(v) if cur is not None and not isinstance(cur, bool) else v)


def load_settings(path: Path | None = None) -> Settings:
    path = path or paths.config_path()
    s = Settings()
    if Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a mapping")
        _merge(s, raw)
    return s.validate()


def save_settings(s: Settings, path: Path | None = None) -> Path:
    s.validate()
    path = Path(path or paths.config_path())
    path.write_text(yaml.safe_dump(dc.asdict(s), allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
