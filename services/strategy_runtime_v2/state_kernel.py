from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Protocol

from services.exec.execution_state_model import (
    FlattenLifecycleState,
    ProtectionState,
    TransitionViolation,
    ensure_flatten_transition,
    ensure_protection_transition,
)

from .runtime_store import RuntimeLifecycle
from .state_transition_event import StateTransitionEvent, build_state_transition_event


RUNTIME_TRANSITIONS: dict[RuntimeLifecycle, set[RuntimeLifecycle]] = {
    RuntimeLifecycle.FLAT: {
        RuntimeLifecycle.PREVIEW,
        RuntimeLifecycle.INTENT_SUBMITTED,
        RuntimeLifecycle.QUARANTINE,
        RuntimeLifecycle.BROKER_UNKNOWN,
    },
    RuntimeLifecycle.PREVIEW: {
        RuntimeLifecycle.FLAT,
        RuntimeLifecycle.INTENT_SUBMITTED,
        RuntimeLifecycle.QUARANTINE,
    },
    RuntimeLifecycle.INTENT_SUBMITTED: {
        RuntimeLifecycle.PENDING_ENTRY,
        RuntimeLifecycle.LIVE,
        RuntimeLifecycle.FLAT,
        RuntimeLifecycle.QUARANTINE,
    },
    RuntimeLifecycle.PENDING_ENTRY: {
        RuntimeLifecycle.LIVE,
        RuntimeLifecycle.FLAT,
        RuntimeLifecycle.QUARANTINE,
        RuntimeLifecycle.BROKER_UNKNOWN,
    },
    RuntimeLifecycle.LIVE: {
        RuntimeLifecycle.PENDING_EXIT,
        RuntimeLifecycle.FLAT,
        RuntimeLifecycle.QUARANTINE,
        RuntimeLifecycle.BROKER_UNKNOWN,
    },
    RuntimeLifecycle.PENDING_EXIT: {
        RuntimeLifecycle.LIVE,
        RuntimeLifecycle.FLAT,
        RuntimeLifecycle.QUARANTINE,
        RuntimeLifecycle.BROKER_UNKNOWN,
    },
    RuntimeLifecycle.QUARANTINE: {
        RuntimeLifecycle.FLAT,
        RuntimeLifecycle.BROKER_UNKNOWN,
    },
    RuntimeLifecycle.BROKER_UNKNOWN: {
        RuntimeLifecycle.FLAT,
        RuntimeLifecycle.QUARANTINE,
        RuntimeLifecycle.PENDING_ENTRY,
        RuntimeLifecycle.LIVE,
    },
}


class OrderLifecycleState(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


ORDER_TRANSITIONS: dict[OrderLifecycleState, set[OrderLifecycleState]] = {
    OrderLifecycleState.CREATED: {
        OrderLifecycleState.SUBMITTED,
        OrderLifecycleState.ACCEPTED,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.FAILED,
    },
    OrderLifecycleState.SUBMITTED: {
        OrderLifecycleState.ACCEPTED,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.FAILED,
    },
    OrderLifecycleState.ACCEPTED: {
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.FAILED,
    },
    OrderLifecycleState.FILLED: set(),
    OrderLifecycleState.CANCELLED: set(),
    OrderLifecycleState.REJECTED: set(),
    OrderLifecycleState.EXPIRED: set(),
    OrderLifecycleState.FAILED: set(),
}


def _coerce_runtime(value: RuntimeLifecycle | str) -> RuntimeLifecycle:
    return value if isinstance(value, RuntimeLifecycle) else RuntimeLifecycle(str(value))


def _coerce_order(value: OrderLifecycleState | str) -> OrderLifecycleState:
    return value if isinstance(value, OrderLifecycleState) else OrderLifecycleState(str(value).strip().lower())


def ensure_runtime_transition(current: RuntimeLifecycle | str, target: RuntimeLifecycle | str) -> None:
    src = _coerce_runtime(current)
    dst = _coerce_runtime(target)
    if dst not in RUNTIME_TRANSITIONS.get(src, set()):
        raise TransitionViolation(f"RuntimeLifecycle illegal transition: {src.value} -> {dst.value}")


def ensure_order_transition(current: OrderLifecycleState | str, target: OrderLifecycleState | str) -> None:
    src = _coerce_order(current)
    dst = _coerce_order(target)
    if dst not in ORDER_TRANSITIONS.get(src, set()):
        raise TransitionViolation(f"OrderLifecycleState illegal transition: {src.value} -> {dst.value}")


class StateTransitionSink(Protocol):
    async def record(self, event: StateTransitionEvent) -> None:
        ...


@dataclass(frozen=True)
class TransitionApplyResult:
    before_state: str
    after_state: str
    event: StateTransitionEvent


class InMemoryStateTransitionRecorder:
    def __init__(self) -> None:
        self._events: list[StateTransitionEvent] = []

    async def record(self, event: StateTransitionEvent) -> None:
        self._events.append(event)

    def events(self) -> tuple[StateTransitionEvent, ...]:
        return tuple(self._events)


class StateKernel:
    async def apply_runtime_transition(
        self,
        *,
        sink: Optional[StateTransitionSink],
        trace_id: str,
        strategy_id: str,
        entity_id: str,
        symbol: str,
        before_state: RuntimeLifecycle | str,
        after_state: RuntimeLifecycle | str,
        event_type: str,
        reason: str,
        source_module: str,
        strategy_run_id: str | None = None,
        trigger_id: str | None = None,
        decision_id: str | None = None,
        order_ref: str | None = None,
        metadata: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> TransitionApplyResult:
        before_v = _coerce_runtime(before_state)
        after_v = _coerce_runtime(after_state)
        ensure_runtime_transition(before_v, after_v)
        event = build_state_transition_event(
            trace_id=trace_id,
            strategy_id=strategy_id,
            strategy_run_id=strategy_run_id,
            trigger_id=trigger_id,
            decision_id=decision_id,
            entity_type="runtime",
            entity_id=entity_id,
            symbol=symbol,
            order_ref=order_ref,
            state_machine="runtime",
            before_state=before_v.value,
            event_type=event_type,
            after_state=after_v.value,
            reason=reason,
            source_module=source_module,
            metadata=metadata,
            payload=payload,
        )
        if sink is not None:
            await sink.record(event)
        return TransitionApplyResult(
            before_state=before_v.value,
            after_state=after_v.value,
            event=event,
        )

    async def apply_flatten_transition(
        self,
        *,
        sink: Optional[StateTransitionSink],
        trace_id: str,
        strategy_id: str,
        entity_id: str,
        before_state: FlattenLifecycleState | str,
        after_state: FlattenLifecycleState | str,
        event_type: str,
        reason: str,
        source_module: str,
        symbol: str | None = None,
        order_ref: str | None = None,
        metadata: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> TransitionApplyResult:
        ensure_flatten_transition(before_state, after_state)
        event = build_state_transition_event(
            trace_id=trace_id,
            strategy_id=strategy_id,
            entity_type="flatten",
            entity_id=entity_id,
            symbol=symbol,
            order_ref=order_ref,
            state_machine="flatten",
            before_state=str(getattr(before_state, "value", before_state)),
            event_type=event_type,
            after_state=str(getattr(after_state, "value", after_state)),
            reason=reason,
            source_module=source_module,
            metadata=metadata,
            payload=payload,
        )
        if sink is not None:
            await sink.record(event)
        return TransitionApplyResult(
            before_state=str(getattr(before_state, "value", before_state)),
            after_state=str(getattr(after_state, "value", after_state)),
            event=event,
        )

    async def apply_order_transition(
        self,
        *,
        sink: Optional[StateTransitionSink],
        trace_id: str,
        strategy_id: str,
        entity_id: str,
        before_state: OrderLifecycleState | str,
        after_state: OrderLifecycleState | str,
        event_type: str,
        reason: str,
        source_module: str,
        symbol: str | None = None,
        order_ref: str | None = None,
        metadata: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> TransitionApplyResult:
        before_v = _coerce_order(before_state)
        after_v = _coerce_order(after_state)
        ensure_order_transition(before_v, after_v)
        event = build_state_transition_event(
            trace_id=trace_id,
            strategy_id=strategy_id,
            entity_type="order",
            entity_id=entity_id,
            symbol=symbol,
            order_ref=order_ref,
            state_machine="order",
            before_state=before_v.value,
            event_type=event_type,
            after_state=after_v.value,
            reason=reason,
            source_module=source_module,
            metadata=metadata,
            payload=payload,
        )
        if sink is not None:
            await sink.record(event)
        return TransitionApplyResult(
            before_state=before_v.value,
            after_state=after_v.value,
            event=event,
        )

    async def apply_protection_transition(
        self,
        *,
        sink: Optional[StateTransitionSink],
        trace_id: str,
        strategy_id: str,
        entity_id: str,
        before_state: ProtectionState | str,
        after_state: ProtectionState | str,
        event_type: str,
        reason: str,
        source_module: str,
        symbol: str | None = None,
        metadata: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> TransitionApplyResult:
        ensure_protection_transition(before_state, after_state)
        event = build_state_transition_event(
            trace_id=trace_id,
            strategy_id=strategy_id,
            entity_type="protection",
            entity_id=entity_id,
            symbol=symbol,
            state_machine="protection",
            before_state=str(getattr(before_state, "value", before_state)),
            event_type=event_type,
            after_state=str(getattr(after_state, "value", after_state)),
            reason=reason,
            source_module=source_module,
            metadata=metadata,
            payload=payload,
        )
        if sink is not None:
            await sink.record(event)
        return TransitionApplyResult(
            before_state=str(getattr(before_state, "value", before_state)),
            after_state=str(getattr(after_state, "value", after_state)),
            event=event,
        )


def runtime_transition_targets(state: RuntimeLifecycle | str) -> tuple[str, ...]:
    src = _coerce_runtime(state)
    return tuple(sorted(item.value for item in RUNTIME_TRANSITIONS.get(src, set())))


def iter_runtime_transitions() -> Iterable[tuple[str, tuple[str, ...]]]:
    for state in RuntimeLifecycle:
        yield state.value, runtime_transition_targets(state)


def order_transition_targets(state: OrderLifecycleState | str) -> tuple[str, ...]:
    src = _coerce_order(state)
    return tuple(sorted(item.value for item in ORDER_TRANSITIONS.get(src, set())))
