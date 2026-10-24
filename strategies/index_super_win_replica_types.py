from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ReplicaTradeState:
    signal_symbol: str
    exec_symbol_long: str
    exec_symbol_short: str
    active_side: Optional[str] = None
    active_exec_symbol: Optional[str] = None
    is_traded: bool = False
    is_flattening: bool = False
    pending_decision_id: Optional[str] = None
    active_trigger_id: Optional[str] = None
    expected_entry_qty: int = 0
    entry_order_ref: Optional[str] = None
    entry_px: Optional[float] = None
    stop_px: Optional[float] = None
    entry_atr_used: Optional[float] = None
    tp1_hit: bool = False
    trail_high_px: Optional[float] = None
    trail_floor_px: Optional[float] = None
    breakeven_done: bool = False
    extras: Dict[str, Any] = field(default_factory=dict)

    def exec_symbol_for_side(self, side: Optional[str]) -> str:
        side_u = str(side or "").strip().upper()
        if side_u == "SHORT":
            return self.exec_symbol_short
        return self.exec_symbol_long


@dataclass(frozen=True)
class ReplicaEntryIntent:
    symbol: str
    side: str
    qty: int
    reason: str
    trigger_id: str
    decision_id: str
    trace_id: str
    order_ref: str
    ref_px: Optional[float] = None
    ref_px_source: Optional[str] = None


@dataclass(frozen=True)
class ReplicaOrderUpdateDecision:
    accepted: bool
    event: str
    reason: str
    runtime_transition: Optional[str] = None


@dataclass(frozen=True)
class ReplicaExitIntent:
    symbol: str
    reason: str
    decision_id: str
    trace_id: str
    order_ref: str
    target_qty: float
    kind: str
    stop_px: Optional[float] = None
    trail_amount: Optional[float] = None
    trail_percent: Optional[float] = None
    trail_stop_px: Optional[float] = None
    lmt_price_offset: Optional[float] = None


@dataclass(frozen=True)
class ReplicaManageDecision:
    action: str
    reason: str
    tp1_qty: int = 0
    add_qty: int = 0
    updated_entry_px: Optional[float] = None
    updated_stop_px: Optional[float] = None
    updated_trail_high_px: Optional[float] = None
    updated_trail_floor_px: Optional[float] = None
