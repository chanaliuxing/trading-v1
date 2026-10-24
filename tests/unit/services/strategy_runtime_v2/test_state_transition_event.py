from __future__ import annotations

import pytest

from services.strategy_runtime_v2 import StateTransitionEvent, build_state_transition_event


def test_build_state_transition_event_creates_normalized_payload() -> None:
    event = build_state_transition_event(
        trace_id=" trace-1 ",
        strategy_id=" qqq_intraday_001 ",
        entity_type="runtime",
        entity_id="qqq_intraday_001:TQQQ",
        state_machine="runtime",
        before_state="pending_entry",
        event_type="broker_fill_confirmed",
        after_state="live",
        reason="trusted_fill",
        source_module="runtime_coordinator",
        symbol="tqqq",
        order_ref=" qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:1 ",
        metadata={"shadow": True},
        payload={"broker_qty": 10},
        event_id="evt-1",
        event_ts_us=12345,
    )
    assert isinstance(event, StateTransitionEvent)
    assert event.trace_id == "trace-1"
    assert event.strategy_id == "qqq_intraday_001"
    assert event.symbol == "tqqq"
    assert event.order_ref == "qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:1"
    assert event.to_dict()["payload"]["broker_qty"] == 10


def test_build_state_transition_event_rejects_missing_required_fields() -> None:
    with pytest.raises(ValueError, match="missing required transition event fields"):
        build_state_transition_event(
            trace_id="",
            strategy_id="qqq_intraday_001",
            entity_type="runtime",
            entity_id="qqq_intraday_001:TQQQ",
            state_machine="runtime",
            before_state="flat",
            event_type="preview_requested",
            after_state="preview",
            reason="preview",
            source_module="runtime_coordinator",
        )
