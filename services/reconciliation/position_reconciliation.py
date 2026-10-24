from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal


@dataclass(frozen=True)
class BrokerAccountPosition:
    """Account-level broker aggregate position truth."""

    account_id: str
    symbol: str
    quantity: Decimal
    avg_cost: Decimal | None = None
    source: str = "broker_snapshot"
    generated_at: datetime | None = None


@dataclass(frozen=True)
class StrategyVirtualPosition:
    """Owner-scoped strategy position truth from the position ledger."""

    account_id: str
    strategy_id: str
    strategy_instance_id: str
    symbol: str
    lot_id: str
    quantity: Decimal
    source: str = "position_ledger"


@dataclass(frozen=True)
class PositionReconciliationResult:
    account_id: str
    symbol: str
    broker_quantity: Decimal | None
    virtual_quantity_sum: Decimal
    matched: bool
    delta: Decimal | None
    status: Literal["MATCHED", "MISMATCH", "UNKNOWN_OWNER", "UNKNOWN_BROKER_TRUTH", "MANUAL_REVIEW_REQUIRED"]
    affected_virtual_positions: list[StrategyVirtualPosition] = field(default_factory=list)
    must_not_mutate_virtual_positions: bool = True


class OvernightCarryStatus(str, Enum):
    OWNER_RESOLVED = "OWNER_RESOLVED"
    UNKNOWN_OWNER = "UNKNOWN_OWNER"
    AMBIGUOUS_OWNER = "AMBIGUOUS_OWNER"
    BROKER_TRUTH_MISSING = "BROKER_TRUTH_MISSING"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


@dataclass(frozen=True)
class OvernightCarryIncident:
    incident_id: str
    account_id: str
    symbol: str
    detected_at: datetime
    broker_quantity: Decimal | None
    strategy_id: str | None
    strategy_instance_id: str | None
    lot_id: str | None
    position_owner_key: str | None
    quantity: Decimal | None
    status: OvernightCarryStatus
    reason: str
    blocks_new_entry: bool = True
    close_only_allowed: bool = True
    requires_manual_review: bool = True


def reconcile_broker_aggregate_to_virtual_positions(
    broker_position: BrokerAccountPosition | None,
    virtual_positions: list[StrategyVirtualPosition],
    *,
    tolerance: Decimal = Decimal("0.000001"),
) -> PositionReconciliationResult:
    """Compare broker account net truth with owner-scoped virtual positions.

    This helper is intentionally read-only. Broker snapshots can validate the
    aggregate sum of strategy owner lanes, but they cannot repair or overwrite
    those lanes because IBKR positions are account+symbol net positions.
    """

    positions = list(virtual_positions or [])
    symbol = _position_symbol(broker_position, positions)
    account_id = _position_account_id(broker_position, positions)
    virtual_sum = sum((p.quantity for p in positions), Decimal("0"))

    if broker_position is None:
        return PositionReconciliationResult(
            account_id=account_id,
            symbol=symbol,
            broker_quantity=None,
            virtual_quantity_sum=virtual_sum,
            matched=False,
            delta=None,
            status="UNKNOWN_BROKER_TRUTH",
            affected_virtual_positions=positions,
        )

    broker_qty = broker_position.quantity
    delta = broker_qty - virtual_sum
    if abs(delta) <= tolerance:
        return PositionReconciliationResult(
            account_id=account_id,
            symbol=symbol,
            broker_quantity=broker_qty,
            virtual_quantity_sum=virtual_sum,
            matched=True,
            delta=Decimal("0"),
            status="MATCHED",
            affected_virtual_positions=positions,
        )

    if not positions and abs(broker_qty) > tolerance:
        status: Literal["UNKNOWN_OWNER", "MANUAL_REVIEW_REQUIRED"] = "UNKNOWN_OWNER"
    else:
        status = "MISMATCH"
    return PositionReconciliationResult(
        account_id=account_id,
        symbol=symbol,
        broker_quantity=broker_qty,
        virtual_quantity_sum=virtual_sum,
        matched=False,
        delta=delta,
        status=status,
        affected_virtual_positions=positions,
    )


def detect_overnight_carry_after_restart(
    *,
    account_id: str,
    broker_positions: list[BrokerAccountPosition] | None,
    virtual_positions: list[StrategyVirtualPosition],
    strategy_policies: dict,
    now: datetime,
    broker_truth_available: bool = True,
) -> list[OvernightCarryIncident]:
    """Detect no-overnight owner lanes that survived a restart.

    The detector is intentionally non-mutating. It creates owner-scoped or
    manual-review incidents and never assigns broker aggregate residuals to an
    active strategy by symbol.
    """

    account = str(account_id or "").strip()
    positions = list(virtual_positions or [])
    incidents: list[OvernightCarryIncident] = []
    if not broker_truth_available:
        return [
            OvernightCarryIncident(
                incident_id=f"overnight_carry:broker_truth_missing:{account}:{now.isoformat()}",
                account_id=account,
                symbol="",
                detected_at=now,
                broker_quantity=None,
                strategy_id=None,
                strategy_instance_id=None,
                lot_id=None,
                position_owner_key=None,
                quantity=None,
                status=OvernightCarryStatus.BROKER_TRUTH_MISSING,
                reason="broker_truth_missing",
            )
        ]

    seen_owner_keys: set[str] = set()
    for position in positions:
        if abs(position.quantity) <= Decimal("0.000001"):
            continue
        if not _policy_requires_flat(position, strategy_policies):
            continue
        owner_key = _position_owner_key(position)
        seen_owner_keys.add(owner_key)
        incidents.append(
            OvernightCarryIncident(
                incident_id=f"overnight_carry:{owner_key}",
                account_id=position.account_id,
                symbol=position.symbol,
                detected_at=now,
                broker_quantity=_broker_qty_for(position, broker_positions or []),
                strategy_id=position.strategy_id,
                strategy_instance_id=position.strategy_instance_id,
                lot_id=position.lot_id,
                position_owner_key=owner_key,
                quantity=position.quantity,
                status=OvernightCarryStatus.OWNER_RESOLVED,
                reason="owner_no_overnight_position_after_restart",
                blocks_new_entry=True,
                close_only_allowed=True,
                requires_manual_review=False,
            )
        )

    for broker_position in broker_positions or []:
        if abs(broker_position.quantity) <= Decimal("0.000001"):
            continue
        same_symbol = [
            item
            for item in positions
            if item.account_id == broker_position.account_id
            and item.symbol.upper() == broker_position.symbol.upper()
            and abs(item.quantity) > Decimal("0.000001")
        ]
        virtual_sum = sum((item.quantity for item in same_symbol), Decimal("0"))
        if not same_symbol:
            incidents.append(
                _overnight_manual_review_incident(
                    account_id=broker_position.account_id,
                    symbol=broker_position.symbol,
                    detected_at=now,
                    broker_quantity=broker_position.quantity,
                    status=OvernightCarryStatus.UNKNOWN_OWNER,
                    reason="overnight_broker_position_without_virtual_owner",
                )
            )
            continue
        if abs(broker_position.quantity - virtual_sum) > Decimal("0.000001"):
            owner_key = f"{broker_position.account_id}:{broker_position.symbol}:broker_virtual_mismatch"
            if owner_key in seen_owner_keys:
                continue
            incidents.append(
                _overnight_manual_review_incident(
                    account_id=broker_position.account_id,
                    symbol=broker_position.symbol,
                    detected_at=now,
                    broker_quantity=broker_position.quantity,
                    status=OvernightCarryStatus.AMBIGUOUS_OWNER if len(same_symbol) > 1 else OvernightCarryStatus.MANUAL_REVIEW_REQUIRED,
                    reason="overnight_broker_virtual_position_mismatch",
                    quantity=broker_position.quantity - virtual_sum,
                )
            )
    return incidents


def _overnight_manual_review_incident(
    *,
    account_id: str,
    symbol: str,
    detected_at: datetime,
    broker_quantity: Decimal | None,
    status: OvernightCarryStatus,
    reason: str,
    quantity: Decimal | None = None,
) -> OvernightCarryIncident:
    return OvernightCarryIncident(
        incident_id=f"overnight_carry:{status.value}:{account_id}:{symbol}",
        account_id=account_id,
        symbol=symbol,
        detected_at=detected_at,
        broker_quantity=broker_quantity,
        strategy_id=None,
        strategy_instance_id=None,
        lot_id=None,
        position_owner_key=None,
        quantity=quantity if quantity is not None else broker_quantity,
        status=status,
        reason=reason,
        blocks_new_entry=True,
        close_only_allowed=True,
        requires_manual_review=True,
    )


def _policy_requires_flat(position: StrategyVirtualPosition, strategy_policies: dict) -> bool:
    candidates = (
        position.strategy_instance_id,
        position.strategy_id,
        f"{position.account_id}:{position.strategy_instance_id}",
        f"{position.account_id}:{position.strategy_id}",
    )
    for key in candidates:
        policy = strategy_policies.get(key)
        if not policy:
            continue
        if isinstance(policy, dict):
            if bool(policy.get("hard_no_overnight") or policy.get("intraday_flat_required") or policy.get("no_overnight")):
                return True
            eod_contract = policy.get("eod_contract")
            if isinstance(eod_contract, dict) and bool(eod_contract.get("no_overnight")):
                return True
        else:
            if bool(getattr(policy, "hard_no_overnight", False) or getattr(policy, "intraday_flat_required", False)):
                return True
            eod_contract = getattr(policy, "eod_contract", None)
            if isinstance(eod_contract, dict) and bool(eod_contract.get("no_overnight")):
                return True
    return False


def _position_owner_key(position: StrategyVirtualPosition) -> str:
    return f"{position.account_id}:{position.strategy_instance_id}:{position.symbol}:{position.lot_id}"


def _broker_qty_for(
    position: StrategyVirtualPosition,
    broker_positions: list[BrokerAccountPosition],
) -> Decimal | None:
    for broker_position in broker_positions:
        if broker_position.account_id == position.account_id and broker_position.symbol.upper() == position.symbol.upper():
            return broker_position.quantity
    return None


def resolve_strategy_owned_flatten_quantity(
    *,
    account_id: str,
    strategy_instance_id: str,
    symbol: str,
    lot_id: str | None,
    ledger_positions: list[StrategyVirtualPosition],
) -> Decimal:
    """Return the owner-ledger quantity eligible for a strategy flatten.

    Strategy flatten must close only the requesting owner lane. Broker account
    aggregate quantity is intentionally not accepted here.
    """

    account = str(account_id or "").strip()
    instance = str(strategy_instance_id or "").strip()
    symbol_u = str(symbol or "").strip().upper()
    lot = str(lot_id or "").strip()
    if not account or not instance or not symbol_u:
        raise ValueError("strategy_flatten_owner_identity_required")

    qty = Decimal("0")
    for position in ledger_positions or []:
        if str(position.account_id or "").strip() != account:
            continue
        if str(position.strategy_instance_id or "").strip() != instance:
            continue
        if str(position.symbol or "").strip().upper() != symbol_u:
            continue
        if lot and str(position.lot_id or "").strip() != lot:
            continue
        qty += position.quantity
    return qty


def _position_symbol(
    broker_position: BrokerAccountPosition | None,
    virtual_positions: list[StrategyVirtualPosition],
) -> str:
    if broker_position is not None:
        return str(broker_position.symbol or "").strip().upper()
    for position in virtual_positions:
        symbol = str(position.symbol or "").strip().upper()
        if symbol:
            return symbol
    return ""


def _position_account_id(
    broker_position: BrokerAccountPosition | None,
    virtual_positions: list[StrategyVirtualPosition],
) -> str:
    if broker_position is not None:
        return str(broker_position.account_id or "").strip()
    for position in virtual_positions:
        account_id = str(position.account_id or "").strip()
        if account_id:
            return account_id
    return ""
