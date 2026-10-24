"""
Execution service data models
"""
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from enum import Enum
import time
import uuid


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"


class OrderType(str, Enum):
    MKT = "MKT"
    LMT = "LMT"
    STP = "STP"
    STP_LMT = "STP_LMT"
    TRAIL = "TRAIL"
    TRAIL_LIMIT = "TRAIL_LIMIT"


class OrderRole(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    SCALE_IN = "SCALE_IN"
    SCALE_OUT = "SCALE_OUT"
    STRATEGY_FLATTEN = "STRATEGY_FLATTEN"
    ACCOUNT_EMERGENCY_FLATTEN = "ACCOUNT_EMERGENCY_FLATTEN"
    MANUAL_BROKER_ORDER = "MANUAL_BROKER_ORDER"
    RECONCILER_DISCOVERED_ORDER = "RECONCILER_DISCOVERED_ORDER"
    UNKNOWN_OWNER = "UNKNOWN_OWNER"


@dataclass(frozen=True)
class OwnerKey:
    """Immutable order owner identity used before broker submission."""

    account_id: str
    strategy_id: str
    strategy_instance_id: str
    symbol: str
    strategy_run_id: Optional[str] = None
    run_id: Optional[str] = None
    lot_id: Optional[str] = None
    intent_id: Optional[str] = None
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    ib_perm_id: Optional[int] = None
    ib_client_id: Optional[int] = None
    ib_connection_epoch: Optional[int] = None


@dataclass(frozen=True)
class IntentFlags:
    """Execution semantics that must be explicit for non-entry orders."""

    reduce_only: bool = False
    close_only: bool = False
    allow_reverse: bool = False
    same_symbol_multi_strategy_safe: bool = False
    intraday_flat_required: bool = False
    hard_no_overnight: bool = False


class FlattenScope(str, Enum):
    STRATEGY_INSTANCE = "STRATEGY_INSTANCE"
    POSITION_OWNER = "POSITION_OWNER"
    ACCOUNT_SYMBOL = "ACCOUNT_SYMBOL"
    ACCOUNT_EMERGENCY = "ACCOUNT_EMERGENCY"


@dataclass(frozen=True)
class FlattenRequest:
    account_id: str
    symbol: str
    scope: FlattenScope
    strategy_id: Optional[str] = None
    strategy_instance_id: Optional[str] = None
    lot_id: Optional[str] = None
    position_owner_key: Optional[str] = None
    order_role: OrderRole = OrderRole.STRATEGY_FLATTEN
    reason: Optional[str] = None
    emergency: bool = False


@dataclass(frozen=True)
class ProtectiveOrderOwner:
    """Owner identity required before a protective fill can mutate position state."""

    account_id: str
    strategy_id: str
    strategy_instance_id: str
    symbol: str
    lot_id: str
    order_role: OrderRole
    parent_order_id: Optional[str] = None
    bracket_id: Optional[str] = None
    oca_group: Optional[str] = None


class QuarantineScope(str, Enum):
    GLOBAL = "GLOBAL"
    ACCOUNT = "ACCOUNT"
    ACCOUNT_SYMBOL = "ACCOUNT_SYMBOL"
    STRATEGY = "STRATEGY"
    STRATEGY_INSTANCE = "STRATEGY_INSTANCE"
    ORDER_OWNER = "ORDER_OWNER"
    BROKER_ORDER = "BROKER_ORDER"


class RebindStatus(str, Enum):
    BOUND_OWNER = "BOUND_OWNER"
    UNKNOWN_OWNER = "UNKNOWN_OWNER"
    AMBIGUOUS_OWNER = "AMBIGUOUS_OWNER"
    STALE_EPOCH = "STALE_EPOCH"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


@dataclass(frozen=True)
class RebindResult:
    status: RebindStatus
    owner: Optional[Mapping[str, Any]] = None
    reason: str = ""
    candidates: int = 0


class BrokerOrderAdjudicationStatus(str, Enum):
    BOUND_SYSTEM_OWNER = "BOUND_SYSTEM_OWNER"
    MANUAL_BROKER_ORDER = "MANUAL_BROKER_ORDER"
    UNKNOWN_OWNER = "UNKNOWN_OWNER"
    AMBIGUOUS_OWNER = "AMBIGUOUS_OWNER"
    FOREIGN_CLIENT_ORDER = "FOREIGN_CLIENT_ORDER"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


@dataclass(frozen=True)
class BrokerOrderAdjudicationResult:
    status: BrokerOrderAdjudicationStatus
    account_id: Optional[str] = None
    symbol: Optional[str] = None
    broker_order_id: Optional[str] = None
    ib_perm_id: Optional[int] = None
    ib_client_id: Optional[int] = None
    order_ref: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_instance_id: Optional[str] = None
    lot_id: Optional[str] = None
    owner_key: Optional[str] = None
    reason: str = ""
    requires_manual_review: bool = True
    allow_auto_cancel: bool = False
    allow_strategy_attribution: bool = False
    allow_pnl_attribution: bool = False
    owner: Optional[Mapping[str, Any]] = None
    candidate_count: int = 0


def _owner_value(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _norm_owner_text(value: Any) -> str:
    return str(value or "").strip()


def _norm_owner_upper(value: Any) -> str:
    return _norm_owner_text(value).upper()


def _quarantine_scope(value: Any) -> Optional[QuarantineScope]:
    raw = _norm_owner_upper(value)
    if not raw:
        return None
    try:
        return QuarantineScope(raw)
    except Exception:
        return None


def quarantine_matches_intent(quarantine: Any, intent: Any, *, live_mode: bool = False) -> bool:
    """Return whether an active quarantine row blocks a new intent.

    Legacy rows without ``quarantine_scope`` are treated as account+symbol when
    both values are available. A symbol-only legacy row is only a conservative
    live-mode block and is never treated as strategy-instance ownership proof.
    """

    status = _norm_owner_upper(_owner_value(quarantine, "status") or "active")
    if status and status != "ACTIVE":
        return False

    scope = _quarantine_scope(_owner_value(quarantine, "quarantine_scope"))
    q_account = _norm_owner_text(_owner_value(quarantine, "account_id"))
    i_account = _norm_owner_text(_owner_value(intent, "account_id"))
    q_symbol = _norm_owner_upper(_owner_value(quarantine, "symbol"))
    i_symbol = _norm_owner_upper(_owner_value(intent, "symbol"))
    q_strategy = _norm_owner_text(_owner_value(quarantine, "strategy_id"))
    i_strategy = _norm_owner_text(_owner_value(intent, "strategy_id"))
    q_instance = _norm_owner_text(_owner_value(quarantine, "strategy_instance_id"))
    i_instance = _norm_owner_text(_owner_value(intent, "strategy_instance_id"))
    q_intent = _norm_owner_text(_owner_value(quarantine, "intent_id"))
    i_intent = _norm_owner_text(_owner_value(intent, "intent_id"))
    q_client_order = _norm_owner_text(_owner_value(quarantine, "client_order_id"))
    i_client_order = _norm_owner_text(_owner_value(intent, "client_order_id"))
    q_broker_order = _norm_owner_text(_owner_value(quarantine, "broker_order_id"))
    i_broker_order = _norm_owner_text(_owner_value(intent, "broker_order_id"))
    q_perm = _norm_owner_text(_owner_value(quarantine, "ib_perm_id") or _owner_value(quarantine, "perm_id"))
    i_perm = _norm_owner_text(_owner_value(intent, "ib_perm_id") or _owner_value(intent, "perm_id"))

    if scope is None:
        if q_account and i_account and q_symbol and i_symbol:
            return q_account == i_account and q_symbol == i_symbol
        if live_mode and q_symbol and i_symbol:
            return q_symbol == i_symbol
        return False

    if scope == QuarantineScope.GLOBAL:
        return True
    if scope == QuarantineScope.ACCOUNT:
        if q_account and i_account:
            return q_account == i_account
        return bool(live_mode and q_account)
    if scope == QuarantineScope.ACCOUNT_SYMBOL:
        if q_account and i_account and q_symbol and i_symbol:
            return q_account == i_account and q_symbol == i_symbol
        return bool(live_mode and q_symbol and i_symbol and q_symbol == i_symbol)
    if scope == QuarantineScope.STRATEGY:
        if q_account and i_account and q_account != i_account:
            return False
        if q_strategy and i_strategy:
            return q_strategy == i_strategy
        return bool(live_mode and q_strategy)
    if scope == QuarantineScope.STRATEGY_INSTANCE:
        if q_account and i_account and q_account != i_account:
            return False
        if q_instance and i_instance:
            return q_instance == i_instance
        return bool(live_mode and q_instance)
    if scope == QuarantineScope.ORDER_OWNER:
        if q_intent and i_intent:
            return q_intent == i_intent
        if q_client_order and i_client_order:
            return q_client_order == i_client_order
        if q_account and i_account and q_symbol and i_symbol and q_instance and i_instance:
            return q_account == i_account and q_symbol == i_symbol and q_instance == i_instance
        return False
    if scope == QuarantineScope.BROKER_ORDER:
        return bool((q_broker_order and i_broker_order and q_broker_order == i_broker_order) or (q_perm and i_perm and q_perm == i_perm))
    return False


def cancel_request_matches_order_owner(cancel_request: Any, order_row: Any) -> bool:
    """Return whether a cancel request may target a durable order owner row.

    This does not inspect ``order_ref`` prefixes or substrings. Cancel ownership
    must come from durable owner fields or exact owner identifiers carried on
    the order row.
    """

    req_strategy = _norm_owner_text(_owner_value(cancel_request, "strategy_id"))
    row_strategy = _norm_owner_text(_owner_value(order_row, "strategy_id"))
    req_symbol = _norm_owner_upper(_owner_value(cancel_request, "symbol"))
    row_symbol = _norm_owner_upper(_owner_value(order_row, "symbol"))
    if not req_strategy or not row_strategy or req_strategy != row_strategy:
        return False
    if not req_symbol or not row_symbol or req_symbol != row_symbol:
        return False

    req_account = _norm_owner_text(_owner_value(cancel_request, "account_id"))
    row_account = _norm_owner_text(_owner_value(order_row, "account_id"))
    if req_account:
        if not row_account or req_account != row_account:
            return False

    req_instance = _norm_owner_text(_owner_value(cancel_request, "strategy_instance_id"))
    row_instance = _norm_owner_text(_owner_value(order_row, "strategy_instance_id"))
    if req_instance:
        if not row_instance or req_instance != row_instance:
            return False

    req_intent = _norm_owner_text(_owner_value(cancel_request, "intent_id"))
    row_intent = _norm_owner_text(_owner_value(order_row, "intent_id"))
    if req_intent and row_intent and req_intent == row_intent:
        return True

    req_client = _norm_owner_text(_owner_value(cancel_request, "client_order_id"))
    row_client = _norm_owner_text(_owner_value(order_row, "client_order_id"))
    if req_client and row_client and req_client == row_client:
        return True

    req_lot = _norm_owner_text(_owner_value(cancel_request, "lot_id") or _owner_value(cancel_request, "position_lot_id"))
    row_lot = _norm_owner_text(_owner_value(order_row, "lot_id") or _owner_value(order_row, "position_lot_id"))
    if req_lot:
        if not row_lot or req_lot != row_lot:
            return False

    if req_instance and row_instance and req_instance == row_instance:
        return True

    return False


def _owner_int(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _broker_perm_id(order: Any) -> Optional[int]:
    return _owner_int(
        _owner_value(order, "ib_perm_id")
        or _owner_value(order, "perm_id")
        or _owner_value(order, "permId")
    )


def _broker_order_id(order: Any) -> str:
    return _norm_owner_text(
        _owner_value(order, "broker_order_id")
        or _owner_value(order, "order_id")
        or _owner_value(order, "orderId")
    )


def _broker_client_id(order: Any) -> Optional[int]:
    return _owner_int(
        _owner_value(order, "ib_client_id")
        or _owner_value(order, "client_id")
        or _owner_value(order, "clientId")
    )


def _broker_epoch(order: Any) -> Optional[int]:
    return _owner_int(_owner_value(order, "ib_connection_epoch") or _owner_value(order, "connection_epoch"))


def _durable_owner_complete(row: Mapping[str, Any]) -> bool:
    return bool(
        _norm_owner_text(row.get("account_id"))
        and _norm_owner_text(row.get("strategy_id"))
        and _norm_owner_text(row.get("strategy_instance_id"))
        and _norm_owner_upper(row.get("symbol"))
    )


def _single_rebind(status: RebindStatus, rows: list[Mapping[str, Any]], reason: str) -> RebindResult:
    if len(rows) == 1:
        return RebindResult(status=RebindStatus.BOUND_OWNER, owner=rows[0], reason=reason, candidates=1)
    if len(rows) > 1:
        return RebindResult(status=RebindStatus.AMBIGUOUS_OWNER, reason=f"ambiguous:{reason}", candidates=len(rows))
    return RebindResult(status=status, reason=reason, candidates=0)


def _owner_key_from_row(row: Mapping[str, Any]) -> str:
    account = _norm_owner_text(row.get("account_id"))
    strategy_instance = _norm_owner_text(row.get("strategy_instance_id"))
    symbol = _norm_owner_upper(row.get("symbol"))
    lot = _norm_owner_text(row.get("lot_id") or row.get("position_lot_id") or "default")
    return f"{account}:{strategy_instance}:{symbol}:{lot}"


def _bound_adjudication(
    broker_order: Any,
    row: Mapping[str, Any],
    *,
    reason: str,
) -> BrokerOrderAdjudicationResult:
    return BrokerOrderAdjudicationResult(
        status=BrokerOrderAdjudicationStatus.BOUND_SYSTEM_OWNER,
        account_id=_norm_owner_text(row.get("account_id") or _owner_value(broker_order, "account_id") or _owner_value(broker_order, "account")) or None,
        symbol=_norm_owner_upper(row.get("symbol") or _owner_value(broker_order, "symbol")) or None,
        broker_order_id=_broker_order_id(broker_order) or _norm_owner_text(row.get("broker_order_id") or row.get("order_id")) or None,
        ib_perm_id=_broker_perm_id(broker_order) or _owner_int(row.get("ib_perm_id") or row.get("perm_id")),
        ib_client_id=_broker_client_id(broker_order) or _owner_int(row.get("ib_client_id") or row.get("client_id")),
        order_ref=_norm_owner_text(_owner_value(broker_order, "order_ref") or _owner_value(broker_order, "orderRef") or row.get("order_ref")) or None,
        strategy_id=_norm_owner_text(row.get("strategy_id")) or None,
        strategy_instance_id=_norm_owner_text(row.get("strategy_instance_id")) or None,
        lot_id=_norm_owner_text(row.get("lot_id") or row.get("position_lot_id")) or None,
        owner_key=_owner_key_from_row(row),
        reason=reason,
        requires_manual_review=False,
        allow_auto_cancel=True,
        allow_strategy_attribution=True,
        allow_pnl_attribution=True,
        owner=row,
        candidate_count=1,
    )


def _manual_adjudication(
    broker_order: Any,
    *,
    status: BrokerOrderAdjudicationStatus,
    reason: str,
    candidate_count: int = 0,
) -> BrokerOrderAdjudicationResult:
    return BrokerOrderAdjudicationResult(
        status=status,
        account_id=_norm_owner_text(_owner_value(broker_order, "account_id") or _owner_value(broker_order, "account")) or None,
        symbol=_norm_owner_upper(_owner_value(broker_order, "symbol")) or None,
        broker_order_id=_broker_order_id(broker_order) or None,
        ib_perm_id=_broker_perm_id(broker_order),
        ib_client_id=_broker_client_id(broker_order),
        order_ref=_norm_owner_text(_owner_value(broker_order, "order_ref") or _owner_value(broker_order, "orderRef")) or None,
        reason=reason,
        candidate_count=candidate_count,
    )


def _single_adjudication(
    broker_order: Any,
    matches: list[Mapping[str, Any]],
    *,
    reason: str,
) -> BrokerOrderAdjudicationResult:
    if len(matches) == 1:
        return _bound_adjudication(broker_order, matches[0], reason=reason)
    if len(matches) > 1:
        return _manual_adjudication(
            broker_order,
            status=BrokerOrderAdjudicationStatus.AMBIGUOUS_OWNER,
            reason=f"ambiguous:{reason}",
            candidate_count=len(matches),
        )
    return _manual_adjudication(
        broker_order,
        status=BrokerOrderAdjudicationStatus.UNKNOWN_OWNER,
        reason=f"no_match:{reason}",
    )


def adjudicate_broker_order_owner(
    broker_order: Any,
    durable_owner_rows: Any,
    active_strategies: Any = None,
    *,
    system_client_ids: Optional[set[int]] = None,
    current_epoch: Optional[int] = None,
) -> BrokerOrderAdjudicationResult:
    """Classify a broker order without assigning ownership by heuristics.

    Active strategies are accepted only as context for callers and are never
    used as ownership proof. Symbol-only, timestamp-only, and order_ref
    prefix/substring matches always remain UNKNOWN_OWNER/manual-review.
    """

    del active_strategies
    rows = [dict(row) for row in list(durable_owner_rows or [])]
    broker_account = _norm_owner_text(_owner_value(broker_order, "account_id") or _owner_value(broker_order, "account"))
    broker_symbol = _norm_owner_upper(_owner_value(broker_order, "symbol"))
    broker_perm = _broker_perm_id(broker_order)
    broker_client = _broker_client_id(broker_order)
    broker_id = _broker_order_id(broker_order)
    broker_ref = _norm_owner_text(_owner_value(broker_order, "order_ref") or _owner_value(broker_order, "orderRef"))
    broker_instance = _norm_owner_text(_owner_value(broker_order, "strategy_instance_id"))
    effective_epoch = current_epoch if current_epoch is not None else _broker_epoch(broker_order)

    if _norm_owner_upper(_owner_value(broker_order, "order_role")) == OrderRole.MANUAL_BROKER_ORDER.value:
        return _manual_adjudication(
            broker_order,
            status=BrokerOrderAdjudicationStatus.MANUAL_BROKER_ORDER,
            reason="manual_broker_order",
        )

    if system_client_ids is not None and broker_client is not None and broker_client not in system_client_ids:
        return _manual_adjudication(
            broker_order,
            status=BrokerOrderAdjudicationStatus.FOREIGN_CLIENT_ORDER,
            reason="foreign_client_id",
        )

    def account_matches(row: Mapping[str, Any]) -> bool:
        row_account = _norm_owner_text(row.get("account_id"))
        return not broker_account or (row_account and row_account == broker_account)

    def symbol_matches(row: Mapping[str, Any]) -> bool:
        row_symbol = _norm_owner_upper(row.get("symbol"))
        return not broker_symbol or (row_symbol and row_symbol == broker_symbol)

    if broker_perm is not None and broker_client is not None:
        matches = [
            row for row in rows
            if _owner_int(row.get("ib_perm_id") or row.get("perm_id")) == broker_perm
            and _owner_int(row.get("ib_client_id") or row.get("client_id")) == broker_client
            and account_matches(row)
        ]
        result = _single_adjudication(broker_order, matches, reason="ib_perm_id_ib_client_id_account")
        if result.status != BrokerOrderAdjudicationStatus.UNKNOWN_OWNER:
            return result

    broker_client_order = _norm_owner_text(_owner_value(broker_order, "client_order_id") or _owner_value(broker_order, "clientOrderId"))
    broker_intent = _norm_owner_text(_owner_value(broker_order, "intent_id"))
    if broker_client_order:
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("client_order_id")) == broker_client_order
            and account_matches(row)
            and symbol_matches(row)
        ]
        result = _single_adjudication(broker_order, matches, reason="client_order_id")
        if result.status != BrokerOrderAdjudicationStatus.UNKNOWN_OWNER:
            return result
    if broker_intent:
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("intent_id") or row.get("idempotency_key")) == broker_intent
            and account_matches(row)
            and symbol_matches(row)
        ]
        result = _single_adjudication(broker_order, matches, reason="intent_id")
        if result.status != BrokerOrderAdjudicationStatus.UNKNOWN_OWNER:
            return result

    if broker_id and broker_client is not None and effective_epoch is not None:
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("broker_order_id") or row.get("order_id")) == broker_id
            and _owner_int(row.get("ib_client_id") or row.get("client_id")) == broker_client
            and _owner_int(row.get("ib_connection_epoch") or row.get("connection_epoch")) == int(effective_epoch)
            and account_matches(row)
        ]
        result = _single_adjudication(broker_order, matches, reason="broker_order_id_client_epoch")
        if result.status != BrokerOrderAdjudicationStatus.UNKNOWN_OWNER:
            return result

    if broker_ref:
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("order_ref")) == broker_ref
            and _durable_owner_complete(row)
            and account_matches(row)
            and symbol_matches(row)
            and (not broker_instance or _norm_owner_text(row.get("strategy_instance_id")) == broker_instance)
        ]
        result = _single_adjudication(broker_order, matches, reason="exact_order_ref_owner_row")
        if result.status != BrokerOrderAdjudicationStatus.UNKNOWN_OWNER:
            return result

    return _manual_adjudication(
        broker_order,
        status=BrokerOrderAdjudicationStatus.UNKNOWN_OWNER,
        reason="UNKNOWN_OWNER_manual_required_no_exact_durable_owner_match",
    )


def rebind_broker_order_to_owner(
    broker_order: Any,
    durable_orders: Any,
    *,
    current_epoch: Optional[int] = None,
) -> RebindResult:
    """Bind a broker open order to a durable owner row without heuristics.

    This deliberately rejects symbol-only, side-only, timestamp-only, and
    order_ref prefix/substring matching. Unknown orders remain unbound for
    quarantine/manual review by callers.
    """

    rows = [dict(row) for row in list(durable_orders or [])]
    if not rows:
        return RebindResult(status=RebindStatus.UNKNOWN_OWNER, reason="no_durable_owner_rows")

    broker_account = _norm_owner_text(_owner_value(broker_order, "account_id") or _owner_value(broker_order, "account"))
    broker_symbol = _norm_owner_upper(_owner_value(broker_order, "symbol"))
    broker_perm = _broker_perm_id(broker_order)
    broker_client = _broker_client_id(broker_order)
    broker_id = _broker_order_id(broker_order)
    broker_ref = _norm_owner_text(_owner_value(broker_order, "order_ref") or _owner_value(broker_order, "orderRef"))
    effective_epoch = current_epoch if current_epoch is not None else _broker_epoch(broker_order)

    def account_matches(row: Mapping[str, Any]) -> bool:
        row_account = _norm_owner_text(row.get("account_id"))
        return not broker_account or (row_account and row_account == broker_account)

    def symbol_matches(row: Mapping[str, Any]) -> bool:
        row_symbol = _norm_owner_upper(row.get("symbol"))
        return not broker_symbol or (row_symbol and row_symbol == broker_symbol)

    if broker_perm is not None and broker_client is not None:
        perm_matches = [
            row
            for row in rows
            if _owner_int(row.get("ib_perm_id") or row.get("perm_id")) == broker_perm
            and _owner_int(row.get("ib_client_id") or row.get("client_id")) == broker_client
            and account_matches(row)
        ]
        result = _single_rebind(RebindStatus.UNKNOWN_OWNER, perm_matches, "ib_perm_id_ib_client_id_account")
        if result.status != RebindStatus.UNKNOWN_OWNER:
            return result

    broker_intent = _norm_owner_text(_owner_value(broker_order, "intent_id"))
    broker_client_order = _norm_owner_text(_owner_value(broker_order, "client_order_id") or _owner_value(broker_order, "clientOrderId"))
    if broker_client_order:
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("client_order_id")) == broker_client_order
            and account_matches(row)
            and symbol_matches(row)
        ]
        result = _single_rebind(RebindStatus.UNKNOWN_OWNER, matches, "client_order_id")
        if result.status != RebindStatus.UNKNOWN_OWNER:
            return result
    if broker_intent:
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("intent_id") or row.get("idempotency_key")) == broker_intent
            and account_matches(row)
            and symbol_matches(row)
        ]
        result = _single_rebind(RebindStatus.UNKNOWN_OWNER, matches, "intent_id")
        if result.status != RebindStatus.UNKNOWN_OWNER:
            return result

    if broker_id and broker_client is not None and effective_epoch is not None:
        stale = [
            row for row in rows
            if _norm_owner_text(row.get("broker_order_id") or row.get("order_id")) == broker_id
            and _owner_int(row.get("ib_client_id") or row.get("client_id")) == broker_client
            and _owner_int(row.get("ib_connection_epoch") or row.get("connection_epoch")) is not None
            and int(_owner_int(row.get("ib_connection_epoch") or row.get("connection_epoch")) or 0) < int(effective_epoch)
        ]
        if stale:
            return RebindResult(status=RebindStatus.STALE_EPOCH, reason="broker_order_id_stale_epoch", candidates=len(stale))
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("broker_order_id") or row.get("order_id")) == broker_id
            and _owner_int(row.get("ib_client_id") or row.get("client_id")) == broker_client
            and _owner_int(row.get("ib_connection_epoch") or row.get("connection_epoch")) == int(effective_epoch)
            and account_matches(row)
        ]
        result = _single_rebind(RebindStatus.UNKNOWN_OWNER, matches, "broker_order_id_client_epoch")
        if result.status != RebindStatus.UNKNOWN_OWNER:
            return result

    if broker_ref:
        matches = [
            row for row in rows
            if _norm_owner_text(row.get("order_ref")) == broker_ref
            and _durable_owner_complete(row)
            and account_matches(row)
            and symbol_matches(row)
        ]
        result = _single_rebind(RebindStatus.UNKNOWN_OWNER, matches, "exact_order_ref_owner_row")
        if result.status != RebindStatus.UNKNOWN_OWNER:
            return result

    if _norm_owner_upper(_owner_value(broker_order, "order_role")) == OrderRole.MANUAL_BROKER_ORDER.value:
        return RebindResult(status=RebindStatus.MANUAL_REVIEW_REQUIRED, reason="manual_broker_order")
    return RebindResult(status=RebindStatus.UNKNOWN_OWNER, reason="no_exact_durable_owner_match")


class OrderState(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class PlacedOrder:
    """Order that has been placed with broker"""
    order_id: int
    order_ref: str
    symbol: str
    side: str
    order_type: str
    qty: float
    limit_px: Optional[float] = None
    stop_px: Optional[float] = None
    trail_amount: Optional[float] = None
    trail_percent: Optional[float] = None
    trail_stop_px: Optional[float] = None
    lmt_price_offset: Optional[float] = None
    tif: str = "DAY"
    oca_group: Optional[str] = None
    status: str = "SUBMITTED"
    broker_order_id: Optional[str] = None
    perm_id: Optional[int] = None
    client_id: Optional[int] = None
    ts_us: int = field(default_factory=lambda: int(time.time() * 1e6))


@dataclass
class OrderIntent:
    """Order intent from strategy"""
    decision_id: str
    strategy_id: str
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType
    account_id: Optional[str] = None
    strategy_instance_id: Optional[str] = None
    intent_id: Optional[str] = None
    client_order_id: Optional[str] = None
    order_role: OrderRole = OrderRole.ENTRY
    reduce_only: bool = False
    close_only: bool = False
    allow_reverse: bool = False
    same_symbol_multi_strategy_safe: bool = False
    ib_connection_epoch: Optional[int] = None
    source_event_id: Optional[str] = None
    limit_px: Optional[float] = None
    stop_px: Optional[float] = None
    tif: str = "DAY"
    order_ref: Optional[str] = None
    oca_group: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.side, OrderSide):
            self.side = OrderSide(str(self.side))
        if not isinstance(self.order_type, OrderType):
            self.order_type = OrderType(str(self.order_type))
        if not isinstance(self.order_role, OrderRole):
            self.order_role = OrderRole(str(self.order_role))
        if not self.order_ref:
            self.order_ref = f"{self.strategy_id}_{uuid.uuid4().hex[:8]}"

    def owner_key(self, *, legacy_test_fallback: bool = False) -> OwnerKey:
        strategy_instance_id = self.strategy_instance_id
        if not strategy_instance_id and legacy_test_fallback:
            # Transitional compatibility only. Live ENTRY validation must not
            # use strategy_id as a silent strategy_instance_id substitute.
            strategy_instance_id = self.strategy_id
        return OwnerKey(
            account_id=self.account_id or "",
            strategy_id=self.strategy_id,
            strategy_instance_id=strategy_instance_id or "",
            symbol=self.symbol,
            intent_id=self.intent_id,
            client_order_id=self.client_order_id,
            ib_connection_epoch=self.ib_connection_epoch,
        )

    def is_close_or_reduce(self) -> bool:
        return bool(
            self.close_only
            or self.reduce_only
            or self.order_role
            in {
                OrderRole.EXIT,
                OrderRole.STOP_LOSS,
                OrderRole.TAKE_PROFIT,
                OrderRole.TRAILING_STOP,
                OrderRole.SCALE_OUT,
                OrderRole.STRATEGY_FLATTEN,
                OrderRole.ACCOUNT_EMERGENCY_FLATTEN,
            }
        )

    def is_entry(self) -> bool:
        return self.order_role in {OrderRole.ENTRY, OrderRole.SCALE_IN} and not self.is_close_or_reduce()

    def requires_owner_identity(self) -> bool:
        return self.order_role not in {
            OrderRole.MANUAL_BROKER_ORDER,
            OrderRole.RECONCILER_DISCOVERED_ORDER,
            OrderRole.UNKNOWN_OWNER,
        }

    def validate_owner_identity(self, *, strict_live: bool = False) -> tuple[bool, str]:
        if (self.close_only or self.reduce_only) and self.allow_reverse:
            return False, "close_or_reduce_only_cannot_allow_reverse"
        if not self.requires_owner_identity():
            return True, "ok"

        missing = []
        if not str(self.account_id or "").strip():
            missing.append("account_id")
        if not str(self.strategy_id or "").strip():
            missing.append("strategy_id")
        if not str(self.symbol or "").strip():
            missing.append("symbol")
        if strict_live and not str(self.strategy_instance_id or "").strip():
            missing.append("strategy_instance_id")
        if self.is_entry() and not (str(self.intent_id or "").strip() or str(self.client_order_id or "").strip()):
            missing.append("intent_id_or_client_order_id")
        if missing:
            return False, f"missing_owner_fields:{','.join(missing)}"
        return True, "ok"


@dataclass
class Fill:
    """Order fill"""
    fill_id: str
    order_id: int
    order_ref: str
    symbol: str
    qty: float
    price: float
    ts_us: int
    side: Optional[str] = None
    commission: float = 0.0
    liquidity: Optional[str] = None
    exchange: Optional[str] = None
    exec_id: Optional[str] = None
    perm_id: Optional[int] = None


@dataclass
class OCOPair:
    """OCO (One-Cancels-Other) order pair for protective orders"""
    parent_order_id: int
    take_profit_order_id: Optional[int] = None
    stop_loss_order_id: Optional[int] = None
    oca_group: str = field(default_factory=lambda: f"OCO_{uuid.uuid4().hex[:8]}")
    status: str = "ACTIVE"  # ACTIVE, PARTIAL_FILLED, FILLED, CANCELLED
