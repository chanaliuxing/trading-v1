from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from services.exec.execution_state_model import FlattenLifecycleState, ProtectionState

from .state_kernel import OrderLifecycleState
from .runtime_store import RuntimeLifecycle


@dataclass(frozen=True)
class CrossStateSnapshot:
    strategy_id: str
    symbol: str
    runtime_state: RuntimeLifecycle
    order_state: Optional[OrderLifecycleState] = None
    flatten_state: Optional[FlattenLifecycleState] = None
    protection_state: Optional[ProtectionState] = None
    broker_qty: Optional[float] = None
    has_owned_live_order: bool = False
    allow_new_entry: bool = False


def validate_cross_state_consistency(snapshot: CrossStateSnapshot) -> tuple[str, ...]:
    issues: list[str] = []

    if (
        snapshot.runtime_state == RuntimeLifecycle.LIVE
        and snapshot.flatten_state == FlattenLifecycleState.CONFIRMED
        and abs(float(snapshot.broker_qty or 0.0)) > 1e-12
    ):
        issues.append("runtime_live_conflicts_with_confirmed_flatten_nonzero_broker_qty")

    if snapshot.runtime_state == RuntimeLifecycle.FLAT and snapshot.has_owned_live_order:
        issues.append("runtime_flat_conflicts_with_owned_live_order")

    if snapshot.protection_state == ProtectionState.FAILED and snapshot.allow_new_entry:
        issues.append("protection_failed_must_block_new_entry")

    if snapshot.flatten_state in {FlattenLifecycleState.PENDING, FlattenLifecycleState.CONFIRMED} and snapshot.allow_new_entry:
        issues.append("flatten_pending_or_confirmed_must_block_new_entry")

    return tuple(issues)


def is_cross_state_consistent(snapshot: CrossStateSnapshot) -> bool:
    return len(validate_cross_state_consistency(snapshot)) == 0
