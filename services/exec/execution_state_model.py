"""
Unified execution integrity state model.

This module defines the canonical state enums and legal transitions for
position lanes, protection lanes, and flatten lifecycle. Strategies and
execution services should converge towards these states instead of each
subsystem inventing local semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Optional, Set


class TransitionViolation(ValueError):
    """Raised when a state transition violates the execution state model."""


class PositionLaneState(str, Enum):
    INTENT_OPEN = "intent_open"
    PENDING_FILL = "pending_fill"
    LIVE = "live"
    PENDING_EXIT = "pending_exit"
    FLATTENING = "flattening"
    CLOSED = "closed"
    QUARANTINED = "quarantined"


class ProtectionState(str, Enum):
    PROTECTED = "protected"
    RECOVERY = "protection_recovery"
    FAILED = "protection_failed"
    FROZEN = "freeze_new_entry"


class FlattenLifecycleState(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


POSITION_LANE_TRANSITIONS: Dict[PositionLaneState, Set[PositionLaneState]] = {
    PositionLaneState.INTENT_OPEN: {
        PositionLaneState.PENDING_FILL,
        PositionLaneState.CLOSED,
        PositionLaneState.QUARANTINED,
    },
    PositionLaneState.PENDING_FILL: {
        PositionLaneState.LIVE,
        PositionLaneState.CLOSED,
        PositionLaneState.QUARANTINED,
    },
    PositionLaneState.LIVE: {
        PositionLaneState.PENDING_EXIT,
        PositionLaneState.FLATTENING,
        PositionLaneState.CLOSED,
        PositionLaneState.QUARANTINED,
    },
    PositionLaneState.PENDING_EXIT: {
        PositionLaneState.LIVE,
        PositionLaneState.FLATTENING,
        PositionLaneState.CLOSED,
        PositionLaneState.QUARANTINED,
    },
    PositionLaneState.FLATTENING: {
        PositionLaneState.CLOSED,
        PositionLaneState.QUARANTINED,
    },
    PositionLaneState.CLOSED: set(),
    PositionLaneState.QUARANTINED: set(),
}


PROTECTION_TRANSITIONS: Dict[ProtectionState, Set[ProtectionState]] = {
    ProtectionState.PROTECTED: {
        ProtectionState.RECOVERY,
        ProtectionState.FAILED,
        ProtectionState.FROZEN,
    },
    ProtectionState.RECOVERY: {
        ProtectionState.PROTECTED,
        ProtectionState.FAILED,
        ProtectionState.FROZEN,
    },
    ProtectionState.FAILED: {
        ProtectionState.FROZEN,
    },
    ProtectionState.FROZEN: {
        ProtectionState.RECOVERY,
        ProtectionState.PROTECTED,
    },
}


FLATTEN_TRANSITIONS: Dict[FlattenLifecycleState, Set[FlattenLifecycleState]] = {
    FlattenLifecycleState.CREATED: {
        FlattenLifecycleState.SUBMITTED,
        FlattenLifecycleState.REJECTED,
        FlattenLifecycleState.CANCELLED,
        FlattenLifecycleState.FAILED,
        FlattenLifecycleState.QUARANTINED,
    },
    FlattenLifecycleState.SUBMITTED: {
        FlattenLifecycleState.PENDING,
        FlattenLifecycleState.CONFIRMED,
        FlattenLifecycleState.REJECTED,
        FlattenLifecycleState.CANCELLED,
        FlattenLifecycleState.FAILED,
        FlattenLifecycleState.QUARANTINED,
    },
    FlattenLifecycleState.PENDING: {
        FlattenLifecycleState.CONFIRMED,
        FlattenLifecycleState.REJECTED,
        FlattenLifecycleState.CANCELLED,
        FlattenLifecycleState.FAILED,
        FlattenLifecycleState.QUARANTINED,
    },
    FlattenLifecycleState.CONFIRMED: set(),
    FlattenLifecycleState.FAILED: set(),
    FlattenLifecycleState.CANCELLED: set(),
    FlattenLifecycleState.REJECTED: set(),
    FlattenLifecycleState.QUARANTINED: set(),
}


@dataclass(frozen=True)
class ExecutionLaneSnapshot:
    strategy_id: str
    symbol: str
    position_state: PositionLaneState
    protection_state: Optional[ProtectionState] = None
    flatten_state: Optional[FlattenLifecycleState] = None


def _normalize_state(state: Enum | str, enum_cls: type[Enum]) -> Enum:
    if isinstance(state, enum_cls):
        return state
    return enum_cls(str(state))


def is_transition_allowed(
    *,
    current: Enum | str,
    target: Enum | str,
    transitions: Dict[Enum, Set[Enum]],
    enum_cls: type[Enum],
) -> bool:
    src = _normalize_state(current, enum_cls)
    dst = _normalize_state(target, enum_cls)
    return dst in transitions.get(src, set())


def ensure_transition(
    *,
    current: Enum | str,
    target: Enum | str,
    transitions: Dict[Enum, Set[Enum]],
    enum_cls: type[Enum],
    context: Optional[str] = None,
) -> None:
    if is_transition_allowed(current=current, target=target, transitions=transitions, enum_cls=enum_cls):
        return
    label = context or getattr(enum_cls, "__name__", "State")
    raise TransitionViolation(f"{label} illegal transition: {current!s} -> {target!s}")


def ensure_position_transition(current: PositionLaneState | str, target: PositionLaneState | str) -> None:
    ensure_transition(
        current=current,
        target=target,
        transitions=POSITION_LANE_TRANSITIONS,
        enum_cls=PositionLaneState,
        context="PositionLaneState",
    )


def ensure_protection_transition(current: ProtectionState | str, target: ProtectionState | str) -> None:
    ensure_transition(
        current=current,
        target=target,
        transitions=PROTECTION_TRANSITIONS,
        enum_cls=ProtectionState,
        context="ProtectionState",
    )


def ensure_flatten_transition(current: FlattenLifecycleState | str, target: FlattenLifecycleState | str) -> None:
    ensure_transition(
        current=current,
        target=target,
        transitions=FLATTEN_TRANSITIONS,
        enum_cls=FlattenLifecycleState,
        context="FlattenLifecycleState",
    )


def terminal_position_states() -> Set[PositionLaneState]:
    return {PositionLaneState.CLOSED, PositionLaneState.QUARANTINED}


def terminal_flatten_states() -> Set[FlattenLifecycleState]:
    return {
        FlattenLifecycleState.CONFIRMED,
        FlattenLifecycleState.FAILED,
        FlattenLifecycleState.CANCELLED,
        FlattenLifecycleState.REJECTED,
        FlattenLifecycleState.QUARANTINED,
    }


def serialize_state_set(states: Iterable[Enum]) -> Set[str]:
    return {str(getattr(state, "value", state)) for state in states}
