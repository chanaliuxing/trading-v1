from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Source = Literal["MANUAL", "IBKR", "CACHE", "MISSING"]
StepStatus = Literal["PASS", "FAIL", "SKIP"]
Severity = Literal["INFO", "WARN", "ERROR"]
SignalStatus = Literal["ALLOW", "NO_TRADE", "NO_SIGNAL"]


def _json_safe(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    value: Any
    source: Source
    ts: str
    is_manual_lock: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": _json_safe(self.value),
            "source": self.source,
            "ts": self.ts,
            "is_manual_lock": self.is_manual_lock,
        }


ResolvedInputs = dict[str, ResolvedInput]


@dataclass(slots=True)
class StepResult:
    step_id: str
    step_title: str
    version: str = "1.0"
    status: StepStatus = "PASS"
    stop: bool = False
    severity: Severity = "INFO"
    inputs_used: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    outputs_set: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    explain: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return _json_safe(raw)


@dataclass(slots=True)
class SignalCard:
    symbol: str
    profile: str
    status: SignalStatus
    market_mode: str | None = None
    bias: str | None = None
    structure: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    reason_chain: list[str] = field(default_factory=list)
    inputs_snapshot: dict[str, Any] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return _json_safe(raw)

