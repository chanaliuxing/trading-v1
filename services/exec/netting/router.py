from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Set


@dataclass(frozen=True)
class RoutingDecision:
    mode: str
    reason: str


@dataclass
class RoutingConfig:
    default_mode: str = "direct"
    strategy_mode_overrides: Dict[str, str] = field(default_factory=dict)
    symbol_mode_overrides: Dict[str, str] = field(default_factory=dict)
    netted_symbols: Set[str] = field(default_factory=set)


class ExecutionRouter:
    """Resolve per-intent execution mode without wiring into runtime."""

    def __init__(self, cfg: Optional[RoutingConfig] = None):
        self.cfg = cfg or RoutingConfig()

    def decide(self, *, strategy_id: str, symbol: str, requested_mode: Optional[str] = None) -> RoutingDecision:
        sid = str(strategy_id or "")
        sym = str(symbol or "").upper()

        if requested_mode:
            req = str(requested_mode).strip().lower()
            if req in {"direct", "netted"}:
                return RoutingDecision(mode=req, reason="strategy_requested")

        mode = self.cfg.strategy_mode_overrides.get(sid)
        if mode in {"direct", "netted"}:
            return RoutingDecision(mode=mode, reason="strategy_override")

        mode = self.cfg.symbol_mode_overrides.get(sym)
        if mode in {"direct", "netted"}:
            return RoutingDecision(mode=mode, reason="symbol_override")

        if self.cfg.netted_symbols and sym in self.cfg.netted_symbols:
            return RoutingDecision(mode="netted", reason="symbol_allowlist")

        default_mode = str(self.cfg.default_mode or "direct").strip().lower()
        if default_mode not in {"direct", "netted"}:
            default_mode = "direct"
        return RoutingDecision(mode=default_mode, reason="default")
