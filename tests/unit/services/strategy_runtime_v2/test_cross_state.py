from __future__ import annotations

from services.exec.execution_state_model import FlattenLifecycleState, ProtectionState
from services.strategy_runtime_v2 import (
    CrossStateSnapshot,
    OrderLifecycleState,
    RuntimeLifecycle,
    is_cross_state_consistent,
    validate_cross_state_consistency,
)


def test_cross_state_rejects_live_runtime_with_confirmed_flatten_and_nonzero_broker_qty() -> None:
    issues = validate_cross_state_consistency(
        CrossStateSnapshot(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            runtime_state=RuntimeLifecycle.LIVE,
            flatten_state=FlattenLifecycleState.CONFIRMED,
            broker_qty=10.0,
        )
    )
    assert "runtime_live_conflicts_with_confirmed_flatten_nonzero_broker_qty" in issues


def test_cross_state_rejects_flat_runtime_when_owned_live_order_exists() -> None:
    issues = validate_cross_state_consistency(
        CrossStateSnapshot(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            runtime_state=RuntimeLifecycle.FLAT,
            has_owned_live_order=True,
        )
    )
    assert issues == ("runtime_flat_conflicts_with_owned_live_order",)


def test_cross_state_rejects_new_entry_when_protection_failed() -> None:
    issues = validate_cross_state_consistency(
        CrossStateSnapshot(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            runtime_state=RuntimeLifecycle.LIVE,
            protection_state=ProtectionState.FAILED,
            allow_new_entry=True,
        )
    )
    assert "protection_failed_must_block_new_entry" in issues


def test_cross_state_rejects_new_entry_while_flatten_pending() -> None:
    issues = validate_cross_state_consistency(
        CrossStateSnapshot(
            strategy_id="qqq_intraday_001",
            symbol="TQQQ",
            runtime_state=RuntimeLifecycle.PENDING_EXIT,
            flatten_state=FlattenLifecycleState.PENDING,
            allow_new_entry=True,
        )
    )
    assert "flatten_pending_or_confirmed_must_block_new_entry" in issues


def test_cross_state_allows_order_accepted_without_runtime_live() -> None:
    snapshot = CrossStateSnapshot(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        runtime_state=RuntimeLifecycle.PENDING_ENTRY,
        order_state=OrderLifecycleState.ACCEPTED,
        broker_qty=0.0,
        allow_new_entry=False,
    )
    assert is_cross_state_consistent(snapshot) is True
