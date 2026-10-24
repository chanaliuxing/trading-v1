from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


@dataclass(frozen=True)
class StateTransitionEvent:
    event_id: str
    event_ts_us: int
    trace_id: str
    strategy_id: str
    entity_type: str
    entity_id: str
    state_machine: str
    before_state: str
    event_type: str
    after_state: str
    reason: str
    source_module: str
    strategy_run_id: Optional[str] = None
    trigger_id: Optional[str] = None
    decision_id: Optional[str] = None
    symbol: Optional[str] = None
    order_ref: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_state_transition_event(
    *,
    trace_id: str,
    strategy_id: str,
    entity_type: str,
    entity_id: str,
    state_machine: str,
    before_state: str,
    event_type: str,
    after_state: str,
    reason: str,
    source_module: str,
    strategy_run_id: Optional[str] = None,
    trigger_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    symbol: Optional[str] = None,
    order_ref: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
    event_id: Optional[str] = None,
    event_ts_us: Optional[int] = None,
) -> StateTransitionEvent:
    trace_id_v = _clean(trace_id)
    strategy_id_v = _clean(strategy_id)
    entity_type_v = _clean(entity_type)
    entity_id_v = _clean(entity_id)
    state_machine_v = _clean(state_machine)
    before_state_v = _clean(before_state)
    event_type_v = _clean(event_type)
    after_state_v = _clean(after_state)
    reason_v = _clean(reason)
    source_module_v = _clean(source_module)
    required = {
        "trace_id": trace_id_v,
        "strategy_id": strategy_id_v,
        "entity_type": entity_type_v,
        "entity_id": entity_id_v,
        "state_machine": state_machine_v,
        "before_state": before_state_v,
        "event_type": event_type_v,
        "after_state": after_state_v,
        "reason": reason_v,
        "source_module": source_module_v,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"missing required transition event fields: {', '.join(sorted(missing))}")
    return StateTransitionEvent(
        event_id=_clean(event_id) or str(uuid.uuid4()),
        event_ts_us=int(event_ts_us or _now_us()),
        trace_id=str(trace_id_v),
        strategy_id=str(strategy_id_v),
        entity_type=str(entity_type_v),
        entity_id=str(entity_id_v),
        state_machine=str(state_machine_v),
        before_state=str(before_state_v),
        event_type=str(event_type_v),
        after_state=str(after_state_v),
        reason=str(reason_v),
        source_module=str(source_module_v),
        strategy_run_id=_clean(strategy_run_id),
        trigger_id=_clean(trigger_id),
        decision_id=_clean(decision_id),
        symbol=_clean(symbol),
        order_ref=_clean(order_ref),
        metadata=dict(metadata or {}),
        payload=dict(payload or {}),
    )
