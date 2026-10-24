from __future__ import annotations

import pytest

from services.exec.execution_state_model import FlattenLifecycleState, ProtectionState, TransitionViolation
from services.strategy_runtime_v2 import (
    InMemoryStateTransitionRecorder,
    OrderLifecycleState,
    RuntimeLifecycle,
    StateKernel,
    ensure_order_transition,
    ensure_runtime_transition,
    order_transition_targets,
    runtime_transition_targets,
)


def test_ensure_runtime_transition_allows_live_to_pending_exit() -> None:
    ensure_runtime_transition(RuntimeLifecycle.LIVE, RuntimeLifecycle.PENDING_EXIT)


def test_ensure_runtime_transition_rejects_flat_to_live() -> None:
    with pytest.raises(TransitionViolation):
        ensure_runtime_transition(RuntimeLifecycle.FLAT, RuntimeLifecycle.LIVE)


def test_ensure_order_transition_allows_submitted_to_accepted() -> None:
    ensure_order_transition(OrderLifecycleState.SUBMITTED, OrderLifecycleState.ACCEPTED)


def test_ensure_order_transition_rejects_filled_to_submitted() -> None:
    with pytest.raises(TransitionViolation):
        ensure_order_transition(OrderLifecycleState.FILLED, OrderLifecycleState.SUBMITTED)


@pytest.mark.asyncio
async def test_state_kernel_records_runtime_transition() -> None:
    sink = InMemoryStateTransitionRecorder()
    kernel = StateKernel()

    result = await kernel.apply_runtime_transition(
        sink=sink,
        trace_id="trace-1",
        strategy_id="qqq_intraday_001",
        entity_id="qqq_intraday_001:TQQQ",
        symbol="TQQQ",
        before_state=RuntimeLifecycle.INTENT_SUBMITTED,
        after_state=RuntimeLifecycle.LIVE,
        event_type="broker_position_confirmed",
        reason="trusted_fill",
        source_module="runtime_coordinator",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:1",
        payload={"broker_qty": 10},
    )

    assert result.before_state == "intent_submitted"
    assert result.after_state == "live"
    assert sink.events()[0].state_machine == "runtime"
    assert sink.events()[0].payload["broker_qty"] == 10


@pytest.mark.asyncio
async def test_state_kernel_records_flatten_and_protection_transition() -> None:
    sink = InMemoryStateTransitionRecorder()
    kernel = StateKernel()

    flatten = await kernel.apply_flatten_transition(
        sink=sink,
        trace_id="trace-flatten",
        strategy_id="qqq_intraday_001",
        entity_id="qqq_intraday_001:TQQQ:flatten",
        before_state=FlattenLifecycleState.SUBMITTED,
        after_state=FlattenLifecycleState.PENDING,
        event_type="flatten_submit_ack",
        reason="awaiting_zero_position",
        source_module="flatten_controller",
        symbol="TQQQ",
    )
    protection = await kernel.apply_protection_transition(
        sink=sink,
        trace_id="trace-protect",
        strategy_id="qqq_intraday_001",
        entity_id="qqq_intraday_001:TQQQ:protection",
        before_state=ProtectionState.RECOVERY,
        after_state=ProtectionState.PROTECTED,
        event_type="protection_recovered",
        reason="stop_rebuilt",
        source_module="protection_controller",
        symbol="TQQQ",
    )

    assert flatten.after_state == "pending"
    assert protection.after_state == "protected"
    assert [event.state_machine for event in sink.events()] == ["flatten", "protection"]


def test_runtime_transition_targets_exposes_contract() -> None:
    assert set(runtime_transition_targets(RuntimeLifecycle.FLAT)) == {
        "broker_unknown",
        "intent_submitted",
        "preview",
        "quarantine",
    }
    assert set(order_transition_targets(OrderLifecycleState.SUBMITTED)) == {
        "accepted",
        "cancelled",
        "expired",
        "failed",
        "filled",
        "rejected",
    }
