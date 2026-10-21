"""Shared dataclasses. Explanations are structured (code + params) and rendered by `i18n`."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SWING, LONGTERM = "swing", "longterm"
STRATEGIES = (SWING, LONGTERM)
KLASS_HIGH, KLASS_STRONG, KLASS_MONITOR, KLASS_LOW = "high", "strong", "monitor", "low"
RISK_ON, NEUTRAL, RISK_OFF = "RISK_ON", "NEUTRAL", "RISK_OFF"
CRITICAL, WARNING, INFO = "critical", "warning", "info"


@dataclass(frozen=True)
class Reason:
    code: str
    params: dict = field(default_factory=dict)
    positive: bool | None = None       # True = supports, False = argues against, None = neutral fact

    def to_dict(self) -> dict:
        return {"code": self.code, "params": self.params, "positive": self.positive}


def _py(v: Any) -> Any:
    return v.item() if hasattr(v, "item") and not isinstance(v, (str, bytes)) else v


def R(code: str, positive: bool | None = None, **params: Any) -> Reason:
    """Build a Reason with plain-Python (JSON-safe) values."""
    return Reason(code, {k: _py(v) for k, v in params.items()}, None if positive is None else bool(_py(positive)))


@dataclass
class TradeLevels:
    entry: float            # reference price = last completed close (actual fill is next open)
    stop: float
    target: float
    atr: float
    support: float | None
    resistance: float | None
    stop_basis: str
    target_basis: str

    @property
    def risk_per_share(self) -> float:
        return self.entry - self.stop

    @property
    def rr(self) -> float:
        r = self.risk_per_share
        return (self.target - self.entry) / r if r > 0 else float("nan")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rr"] = self.rr
        return d


@dataclass
class ComponentScore:
    key: str
    points: float
    max_points: float
    reasons: list[Reason] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"key": self.key, "points": round(self.points, 2), "max": self.max_points,
                "reasons": [r.to_dict() for r in self.reasons]}


@dataclass
class ScoreResult:
    strategy: str
    total: float
    klass: str
    validated: bool
    failures: list[Reason] = field(default_factory=list)       # validation rules that failed
    components: dict[str, ComponentScore] = field(default_factory=dict)
    levels: TradeLevels | None = None
    setups: list[str] = field(default_factory=list)            # core setups present
    experimental: list[str] = field(default_factory=list)      # experimental patterns (not scored)
    version: str = ""

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "total": round(self.total, 2), "klass": self.klass,
                "validated": self.validated, "failures": [r.to_dict() for r in self.failures],
                "components": {k: v.to_dict() for k, v in self.components.items()},
                "levels": self.levels.to_dict() if self.levels else None,
                "setups": self.setups, "experimental": self.experimental, "version": self.version}


@dataclass
class Alert:
    key: str                # dedupe key
    severity: str
    kind: str
    title: str
    body: str
    symbol: str = ""
