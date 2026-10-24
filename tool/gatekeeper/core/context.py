from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.cache import GatekeeperCache
from core.types import ResolvedInput, ResolvedInputs


@dataclass(slots=True)
class Constraints:
    forbidden_structures: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GatekeeperState:
    flags: dict[str, Any] = field(default_factory=dict)
    market_mode: str | None = None
    bias: str | None = None
    structure: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    constraints: Constraints = field(default_factory=Constraints)


@dataclass(slots=True)
class GatekeeperContext:
    resolved_inputs: ResolvedInputs
    config: dict[str, Any]
    cache: GatekeeperCache
    state: GatekeeperState
    symbol: str
    anchor_symbol: str
    profile: str

    def input(self, key: str) -> ResolvedInput | None:
        return self.resolved_inputs.get(key)

    def value(self, key: str, default: Any = None) -> Any:
        ri = self.resolved_inputs.get(key)
        if ri is None:
            return default
        if ri.source == "MISSING":
            return default
        return ri.value

    def is_missing(self, key: str) -> bool:
        ri = self.resolved_inputs.get(key)
        return ri is None or ri.source == "MISSING"

