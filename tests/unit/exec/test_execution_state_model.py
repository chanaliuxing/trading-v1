from __future__ import annotations

import pytest

from services.exec.execution_state_model import (
    ExecutionLaneSnapshot,
    FlattenLifecycleState,
    PositionLaneState,
    ProtectionState,
    TransitionViolation,
    ensure_flatten_transition,
    ensure_position_transition,
    ensure_protection_transition,
    serialize_state_set,
    terminal_flatten_states,
    terminal_position_states,
)


def test_position_lane_allows_intent_open_to_pending_fill():
    ensure_position_transition(PositionLaneState.INTENT_OPEN, PositionLaneState.PENDING_FILL)


def test_position_lane_rejects_pending_fill_to_flattening():
    with pytest.raises(TransitionViolation):
        ensure_position_transition(PositionLaneState.PENDING_FILL, PositionLaneState.FLATTENING)


def test_position_lane_rejects_accepted_style_shortcut_to_live():
    with pytest.raises(TransitionViolation):
        ensure_position_transition(PositionLaneState.INTENT_OPEN, PositionLaneState.LIVE)


def test_protection_state_allows_recovery_back_to_protected():
    ensure_protection_transition(ProtectionState.RECOVERY, ProtectionState.PROTECTED)


def test_protection_state_rejects_failed_to_protected_directly():
    with pytest.raises(TransitionViolation):
        ensure_protection_transition(ProtectionState.FAILED, ProtectionState.PROTECTED)


def test_flatten_lifecycle_allows_submitted_to_confirmed():
    ensure_flatten_transition(FlattenLifecycleState.SUBMITTED, FlattenLifecycleState.CONFIRMED)


def test_flatten_lifecycle_rejects_confirmed_to_pending():
    with pytest.raises(TransitionViolation):
        ensure_flatten_transition(FlattenLifecycleState.CONFIRMED, FlattenLifecycleState.PENDING)


def test_terminal_state_helpers_return_expected_values():
    assert serialize_state_set(terminal_position_states()) == {"closed", "quarantined"}
    assert serialize_state_set(terminal_flatten_states()) == {
        "confirmed",
        "failed",
        "cancelled",
        "rejected",
        "quarantined",
    }


def test_execution_lane_snapshot_captures_unified_state():
    snap = ExecutionLaneSnapshot(
        strategy_id="spy_1dte_ma10_reject_0324_001",
        symbol="OPT:SPY:20260402:655:C",
        position_state=PositionLaneState.FLATTENING,
        protection_state=ProtectionState.FROZEN,
        flatten_state=FlattenLifecycleState.PENDING,
    )

    assert snap.position_state is PositionLaneState.FLATTENING
    assert snap.protection_state is ProtectionState.FROZEN
    assert snap.flatten_state is FlattenLifecycleState.PENDING
