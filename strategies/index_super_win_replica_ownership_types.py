from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class ReplicaLotRecord:
    strategy_id: str
    position_lot_id: str
    signal_symbol: str
    exec_symbol: str
    side: str
    planned_qty: float
    live_qty: float
    remaining_qty: float
    opened_at: datetime
    entry_decision_id: str = ""
    entry_trigger_id: str = ""
    entry_order_ref: str = ""
    entry_broker_order_id: str = ""
    entry_perm_id: Optional[int] = None
    status: str = "intent_open"
    last_exit_reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplicaOwnershipIntentRecord:
    strategy_id: str
    decision_id: str
    trigger_id: str
    signal_symbol: str
    exec_symbol: str
    side: str
    qty: float
    ts_bar_end_us: int
    position_lot_id: str
    order_ref: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplicaExitFillRecord:
    strategy_id: str
    position_lot_id: str
    qty: float
    reason: str
    ts: datetime
    order_ref: str = ""
    broker_order_id: str = ""
    perm_id: Optional[int] = None
    payload: Dict[str, Any] = field(default_factory=dict)
