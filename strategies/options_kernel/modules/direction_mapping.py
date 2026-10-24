from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol

from strategies.options_kernel.types import Signal


@dataclass(frozen=True)
class DirectionMappingContext:
    signal: Signal
    extras: Dict[str, Any] = field(default_factory=dict)


class BaseDirectionMappingPolicy(Protocol):
    policy_id: str

    def map_signal_side(self, ctx: DirectionMappingContext) -> str:
        ...


class OptionNativeDirectionMappingPolicy:
    def __init__(self, policy_id: str = "option_native_signal") -> None:
        self.policy_id = policy_id

    def map_signal_side(self, ctx: DirectionMappingContext) -> str:
        return str(ctx.signal.side or "").strip().upper()


class StockSignalOptionDirectionMappingPolicy:
    def __init__(self, policy_id: str = "stock_signal_to_option_side") -> None:
        self.policy_id = policy_id

    def map_signal_side(self, ctx: DirectionMappingContext) -> str:
        side = str(ctx.signal.side or "").strip().upper()
        if side in {"CALL", "PUT"}:
            return side
        if side in {"LONG", "BUY"}:
            return "CALL"
        if side in {"SHORT", "SELL"}:
            return "PUT"
        return side
