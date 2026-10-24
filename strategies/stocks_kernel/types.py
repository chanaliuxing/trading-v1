from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class PositionLotStatus:
    PENDING_ENTRY = "pending_entry"
    LIVE = "live"
    PENDING_EXIT = "pending_exit"
    CLOSED = "closed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class KernelExecutionLane:
    lane_id: str
    symbol: str
    side: Optional[str] = None
    role: str = "execution"
    close_only_default: bool = False
    ownership_scope: str = "strategy"
    flatten_authority: str = "strategy"
    protection_authority: str = "strategy"
    adopt_authority: str = "strategy"
    execution_preference: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KernelInstrumentBinding:
    signal_symbol: str
    long_exec_symbol: str
    short_exec_symbol: str
    confirm_symbol: Optional[str] = None
    volatility_symbol: Optional[str] = None
    execution_lanes: tuple[KernelExecutionLane, ...] = ()
    signal_lanes: tuple[KernelExecutionLane, ...] = ()
    confirm_lanes: tuple[KernelExecutionLane, ...] = ()
    protection_lanes: tuple[KernelExecutionLane, ...] = ()
    hedge_lanes: tuple[KernelExecutionLane, ...] = ()
    strategy_namespace: Optional[str] = None
    ownership_scope: str = "strategy"

    def exec_symbols(self) -> tuple[str, ...]:
        lane_symbols = tuple(
            sorted(
                {
                    str(lane.symbol or "").strip().upper()
                    for lane in self.execution_lanes
                    if str(lane.role or "").strip().lower() == "execution"
                }
                - {""}
            )
        )
        if lane_symbols:
            return lane_symbols
        fallback = tuple(sorted({self.long_exec_symbol, self.short_exec_symbol} - {""}))
        return fallback

    def lanes(self, role: str | None = None) -> tuple[KernelExecutionLane, ...]:
        if role is None:
            combined = tuple(self.signal_lanes) + tuple(self.execution_lanes) + tuple(self.confirm_lanes) + tuple(self.protection_lanes) + tuple(self.hedge_lanes)
            if combined:
                return combined
            return tuple(lane for lane in (self.lane_for_side("LONG"), self.lane_for_side("SHORT")) if lane is not None)
        role_v = str(role or "").strip().lower()
        if role_v == "signal":
            return tuple(self.signal_lanes)
        if role_v == "execution":
            return tuple(self.execution_lanes) or tuple(
                lane for lane in (self.lane_for_side("LONG"), self.lane_for_side("SHORT")) if lane is not None
            )
        if role_v == "confirm":
            return tuple(self.confirm_lanes)
        if role_v == "protection":
            return tuple(self.protection_lanes)
        if role_v == "hedge":
            return tuple(self.hedge_lanes)
        return ()

    def lane_by_id(self, lane_id: str) -> Optional[KernelExecutionLane]:
        lane_id_v = str(lane_id or "").strip()
        if not lane_id_v:
            return None
        for lane in self.lanes():
            if str(lane.lane_id or "").strip() == lane_id_v:
                return lane
        return None

    def lane_for_side(self, side: str) -> Optional[KernelExecutionLane]:
        side_u = str(side or "").strip().upper()
        if side_u not in {"LONG", "SHORT"}:
            return None
        for lane in self.execution_lanes:
            if str(lane.side or "").strip().upper() == side_u and str(lane.role or "").strip().lower() == "execution":
                return lane
        symbol = self.long_exec_symbol if side_u == "LONG" else self.short_exec_symbol
        if not symbol:
            return None
        return KernelExecutionLane(
            lane_id=f"{side_u.lower()}_exec",
            symbol=symbol,
            side=side_u,
            role="execution",
        )


@dataclass
class PositionLot:
    position_lot_id: str
    strategy_id: str
    symbol: str
    side: str
    opened_by_decision_id: str
    opened_by_trigger_id: Optional[str]
    entry_order_ref: str
    entry_order_id: Optional[str] = None
    entry_perm_id: Optional[int] = None
    live_qty: float = 0.0
    filled_qty: float = 0.0
    avg_entry_price: Optional[float] = None
    state: str = PositionLotStatus.PENDING_ENTRY
    lane_id: Optional[str] = None
    ownership_scope: str = "strategy"
    flatten_authority: str = "strategy"
    protection_authority: str = "strategy"
    adopt_authority: str = "strategy"
    protection_order_refs: list[str] = field(default_factory=list)
    flatten_order_refs: list[str] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KernelSymbolExposureView:
    strategy_id: str
    symbol: str
    long_qty: float
    short_qty: float
    net_qty: float
    open_lot_count: int


@dataclass(frozen=True)
class KernelFillReport:
    symbol: str
    broker_order_id: Optional[str] = None
    perm_id: Optional[int] = None
    order_ref: Optional[str] = None
    qty: float = 0.0
    price: Optional[float] = None
    exec_id: Optional[str] = None


@dataclass(frozen=True)
class FillAttributionResult:
    position_lot_id: Optional[str]
    strategy_id: Optional[str]
    reason: str
    quarantine_required: bool = False


@dataclass(frozen=True)
class LotOrderBinding:
    position_lot_id: str
    strategy_id: str
    symbol: str
    order_role: str
    order_ref: str
    lane_id: Optional[str] = None
    ownership_scope: str = "strategy"
    broker_order_id: Optional[str] = None
    perm_id: Optional[int] = None


@dataclass(frozen=True)
class LotProtectionChainView:
    position_lot_id: str
    strategy_id: str
    symbol: str
    entry_order_ref: Optional[str]
    stop_order_refs: tuple[str, ...]
    flatten_order_refs: tuple[str, ...]


@dataclass(frozen=True)
class KernelSubmitResult:
    position_lot_id: str
    order_ref: str
    strategy_id: str
    symbol: str
    side: str
    lane_id: Optional[str] = None


@dataclass(frozen=True)
class KernelFillApplyResult:
    position_lot_id: Optional[str]
    strategy_id: Optional[str]
    symbol: str
    order_role: Optional[str]
    action: str
    reason: str
    quarantine_required: bool = False


@dataclass(frozen=True)
class KernelExecutionEvent:
    stage: str
    strategy_id: Optional[str]
    symbol: str
    domain: str = "system"
    component: str = "stocks_kernel"
    severity: str = "info"
    status: str = "observed"
    strategy_run_id: Optional[str] = None
    trigger_id: Optional[str] = None
    decision_id: Optional[str] = None
    trace_id: Optional[str] = None
    intent_id: Optional[str] = None
    order_id: Optional[str] = None
    position_lot_id: Optional[str] = None
    lane_id: Optional[str] = None
    order_ref: Optional[str] = None
    order_role: Optional[str] = None
    broker_order_id: Optional[str] = None
    perm_id: Optional[int] = None
    reason: Optional[str] = None
    ownership_scope: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    ts_epoch_s: float
    last_price: Optional[float] = None
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    session: Optional[str] = None
    vwap: Optional[float] = None
    minute_open: Optional[float] = None
    minute_high: Optional[float] = None
    minute_low: Optional[float] = None
    minute_close: Optional[float] = None
    minute_volume: Optional[float] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoryBar:
    ts_epoch_s: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class HistoryBundle:
    symbol: str
    bars_1m: tuple[HistoryBar, ...] = ()


@dataclass(frozen=True)
class DataReadinessReport:
    ready: bool
    reason: str
    freshness_ok: bool
    history_ready: bool
    snapshot_ready: bool
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureResult:
    value: Any
    ready: bool
    reason: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureContext:
    symbol: str
    snapshot: MarketSnapshot
    history: HistoryBundle
    values: Dict[str, Any]
    readiness: Dict[str, str] = field(default_factory=dict)
    scope: str = "signal"


@dataclass(frozen=True)
class SymbolDataContext:
    scope: str
    symbol: str
    snapshot: Optional[MarketSnapshot]
    history: Optional[HistoryBundle]
    readiness: Optional[DataReadinessReport] = None
    feature_context: Optional[FeatureContext] = None


@dataclass(frozen=True)
class DataContextBundle:
    signal: SymbolDataContext
    execution: Dict[str, SymbolDataContext] = field(default_factory=dict)
    confirm: Dict[str, SymbolDataContext] = field(default_factory=dict)
    hedge: Dict[str, SymbolDataContext] = field(default_factory=dict)

    def scopes(self) -> Dict[str, Dict[str, SymbolDataContext]]:
        return {
            "signal": {self.signal.symbol: self.signal},
            "execution": dict(self.execution),
            "confirm": dict(self.confirm),
            "hedge": dict(self.hedge),
        }

    def symbols_for_scope(self, scope: str) -> tuple[str, ...]:
        scope_v = str(scope or "").strip().lower()
        if scope_v == "signal":
            return (self.signal.symbol,)
        mapping = self.scopes().get(scope_v, {})
        return tuple(sorted(str(symbol or "").strip().upper() for symbol in mapping.keys() if str(symbol or "").strip()))

    def context_for(self, *, scope: str, symbol: str | None = None) -> Optional[SymbolDataContext]:
        scope_v = str(scope or "").strip().lower()
        symbol_v = str(symbol or "").strip().upper()
        if scope_v == "signal":
            return self.signal if (not symbol_v or symbol_v == self.signal.symbol) else None
        mapping = self.scopes().get(scope_v, {})
        if not symbol_v:
            if len(mapping) == 1:
                return next(iter(mapping.values()))
            return None
        return mapping.get(symbol_v)

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for scope, mapping in self.scopes().items():
            out[scope] = {
                "symbols": tuple(sorted(mapping.keys())),
                "ready_symbols": tuple(
                    sorted(
                        symbol
                        for symbol, ctx in mapping.items()
                        if bool(getattr(getattr(ctx, "readiness", None), "ready", False))
                    )
                ),
            }
        return out


@dataclass(frozen=True)
class GateDecision:
    gate_id: str
    allowed: bool
    reason: str
    trader_reason_code: str
    severity: str = "info"
    recoverable: bool = False
    block_scope: str = "strategy"
    retry_after_sec: Optional[float] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateEvaluationReport:
    allowed: bool
    decisions: tuple[GateDecision, ...]
    blocked_by: Optional[str] = None
    fail_mode: str = "fail_closed"


@dataclass(frozen=True)
class SignalIntent:
    action: str
    side: Optional[str]
    symbol: str
    confidence: Optional[float] = None
    signal_context: Dict[str, Any] = field(default_factory=dict)
    inhibition_reasons: tuple[str, ...] = ()
    payload: Dict[str, Any] = field(default_factory=dict)


class ActionType:
    ENTRY = "ENTRY"
    ADD = "ADD"
    REDUCE = "REDUCE"
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    RETREAT = "RETREAT"
    FLATTEN = "FLATTEN"
    CANCEL_WORKING = "CANCEL_WORKING"
    ENSURE_PROTECTION = "ENSURE_PROTECTION"


@dataclass(frozen=True)
class ActionPlan:
    action_type: str
    symbol: str
    side: Optional[str] = None
    qty: Optional[float] = None
    priority: int = 100
    lane_id: Optional[str] = None
    position_lot_id: Optional[str] = None
    ownership_scope: str = "strategy"
    close_only: bool = False
    flatten_action: bool = False
    reduce_only: bool = False
    protection_attached: bool = False
    trigger_id: Optional[str] = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryPlan:
    symbol: str
    side: str
    qty: float
    lane_id: Optional[str] = None
    ownership_scope: str = "strategy"
    trigger_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExitPlan:
    symbol: str
    order_role: str
    qty: float
    position_lot_id: Optional[str] = None
    lane_id: Optional[str] = None
    ownership_scope: str = "strategy"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    avg_price: Optional[float] = None
    account: Optional[str] = None


@dataclass(frozen=True)
class BrokerOpenOrder:
    symbol: str
    order_ref: str
    lane_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    perm_id: Optional[int] = None
    side: Optional[str] = None
    qty: Optional[float] = None
    order_role: Optional[str] = None


@dataclass(frozen=True)
class BrokerSyncReport:
    ok: bool
    reason: str
    foreign_positions: tuple[BrokerPosition, ...] = ()
    foreign_orders: tuple[BrokerOpenOrder, ...] = ()
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KernelRuntimeSnapshot:
    strategy_id: str
    order_namespace: str
    lots: tuple[PositionLot, ...]
    bindings: tuple[LotOrderBinding, ...]
    payload: Dict[str, Any] = field(default_factory=dict)
