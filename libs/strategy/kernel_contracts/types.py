from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class KernelBarContext:
    ts: datetime
    symbol: str
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    vwap: float = 0.0
    atr: float = 0.0
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    ts: datetime
    side: str
    signal_kind: str
    grade: str = ""
    regime: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryPlan:
    ts: datetime
    side: str
    signal_kind: str
    structure_stop: float
    target_1r: float
    target_1p2r: float
    r_per_share: float
    contracts: int
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryIntent:
    ts: datetime
    strategy_id: str
    underlying_symbol: str
    option_symbol: str
    side: str
    qty: float
    signal_kind: str = ""
    order_role: str = "ENTRY"
    position_lot_id: str = ""
    decision_id: str = ""
    order_ref: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Selection:
    opt_symbol: str
    expiry: str
    strike: float
    side: str
    bid: float
    ask: float
    mid: float
    last: float = 0.0
    quote_age_sec: float = 0.0
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionProfile:
    profile_id: str
    market_data_profile_id: str
    timeout_bars: int
    dynamic_reprice_to_ask: bool = False
    max_premium_expansion_pct: float = 0.0
    partial_take_profit_contracts: int = 0
    stop_rule: str = ""
    time_stop_soft_bars: int = 0
    time_stop_hard_bars: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class KernelPositionState:
    strategy_id: str
    position_lot_id: str
    opt_symbol: str
    side: str
    qty: float
    remaining_qty: float
    opened_at: datetime
    avg_entry_px: float = 0.0
    structure_stop: float = 0.0
    target_1r: float = 0.0
    target_1p2r: float = 0.0
    status: str = "flat"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionLot:
    strategy_id: str
    position_lot_id: str
    symbol: str
    side: str
    qty: float
    opened_at: datetime
    entry_decision_id: str = ""
    entry_trigger_id: str = ""
    entry_snapshot_id: str = ""
    execution_profile_id: str = ""
    order_ref: str = ""
    status: str = "opening"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExitPlan:
    ts: datetime
    position_lot_id: str
    qty: float
    reason: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IBKRTrailingStopOrderSpec:
    action: str
    orderType: str
    totalQuantity: float
    trailingPercent: Optional[float] = None
    auxPrice: Optional[float] = None
    trailStopPrice: Optional[float] = None


@dataclass(frozen=True)
class UnderlyingTriggeredOptionExitPlan:
    underlying_symbol: str
    trigger_price: float
    trigger_direction: str
    option_contract_id: Optional[str]
    exit_action: str
    quantity: float
    fallback_order_type: str = "MARKET"
    ibkr_trailing_order_spec: Optional[IBKRTrailingStopOrderSpec] = None
    underlying_stop_reference: Optional[float] = None
    option_stop_order_spec: Optional[Dict[str, Any]] = None
    conversion_policy: str = "UNDERLYING_TRIGGERED_OPTION_EXIT"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "trigger_price": self.trigger_price,
            "trigger_direction": self.trigger_direction,
            "option_contract_id": self.option_contract_id,
            "exit_action": self.exit_action,
            "quantity": self.quantity,
            "fallback_order_type": self.fallback_order_type,
            "ibkr_trailing_order_spec": (
                None
                if self.ibkr_trailing_order_spec is None
                else dict(self.ibkr_trailing_order_spec.__dict__)
            ),
            "underlying_stop_reference": self.underlying_stop_reference,
            "option_stop_order_spec": dict(self.option_stop_order_spec or {}),
            "conversion_policy": self.conversion_policy,
        }

    def to_exit_plan(
        self,
        *,
        ts: datetime,
        position_lot_id: str,
        reason: str = "UNDERLYING_TRIGGERED_OPTION_EXIT",
    ) -> ExitPlan:
        return ExitPlan(
            ts=ts,
            position_lot_id=str(position_lot_id),
            qty=float(self.quantity),
            reason=str(reason or self.conversion_policy),
            payload={
                "exit_model": self.conversion_policy,
                "underlying_trigger": {
                    "underlying_symbol": self.underlying_symbol,
                    "trigger_price": self.trigger_price,
                    "trigger_direction": self.trigger_direction,
                    "underlying_stop_reference": self.underlying_stop_reference,
                },
                "option_contract_id": self.option_contract_id,
                "exit_action": self.exit_action,
                "fallback_order_type": self.fallback_order_type,
                "ibkr_trailing_order_spec": (
                    None
                    if self.ibkr_trailing_order_spec is None
                    else dict(self.ibkr_trailing_order_spec.__dict__)
                ),
                "option_stop_order_spec": dict(self.option_stop_order_spec or {}),
            },
        )


@dataclass(frozen=True)
class OrderAck:
    status: str
    intent_action: str
    strategy_id: str
    symbol: str
    decision_id: str = ""
    order_ref: str = ""
    position_lot_id: str = ""
    broker_order_id: Optional[int] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderReject:
    reason: str
    intent_action: str
    strategy_id: str
    symbol: str = ""
    decision_id: str = ""
    order_ref: str = ""
    position_lot_id: str = ""
    stage: str = ""
    reasons: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginRuntimeState:
    strategy_id: str
    kernel_mode: str
    symbol: str
    now: datetime
    wall_now: datetime
    spot: float
    bar: KernelBarContext
    features: Dict[str, Any] = field(default_factory=dict)
    active_position: Optional[KernelPositionState] = None
    active_positions: List[KernelPositionState] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParityDiff:
    field: str
    old_value: Any
    new_value: Any
    severity: str = "error"


@dataclass(frozen=True)
class KernelIntentRecord:
    strategy_id: str
    decision_id: str
    symbol: str
    target_qty: float
    ts_bar_end_us: int
    order_ref: str = ""
    position_lot_id: str = ""
    decision_snapshot_id: str = ""
    execution_profile_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
