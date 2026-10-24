from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.strategy_runtime_v2 import (
    CallbackStateTransitionSink,
    LocalAuditStateTransitionSink,
    build_state_transition_event,
)


@pytest.mark.asyncio
async def test_local_audit_state_transition_sink_emits_audit_record() -> None:
    writer = MagicMock()
    sink = LocalAuditStateTransitionSink(audit_writer=writer, component="runtime_state_test")
    event = build_state_transition_event(
        trace_id="trace-1",
        strategy_id="qqq_intraday_001",
        entity_type="runtime",
        entity_id="qqq_intraday_001:TQQQ",
        state_machine="runtime",
        before_state="intent_submitted",
        event_type="broker_position_confirmed",
        after_state="live",
        reason="trusted_fill",
        source_module="runtime_coordinator",
        symbol="TQQQ",
    )

    await sink.record(event)

    writer.emit.assert_called_once()
    kwargs = writer.emit.call_args.kwargs
    assert kwargs["domain"] == "state"
    assert kwargs["stage"] == "runtime_transition"
    assert kwargs["status"] == "live"


@pytest.mark.asyncio
async def test_callback_state_transition_sink_invokes_callback() -> None:
    seen = []
    sink = CallbackStateTransitionSink(lambda event: seen.append(event.after_state))
    event = build_state_transition_event(
        trace_id="trace-2",
        strategy_id="qqq_intraday_001",
        entity_type="flatten",
        entity_id="flatten-1",
        state_machine="flatten",
        before_state="submitted",
        event_type="flatten_confirmed",
        after_state="confirmed",
        reason="runtime_reconciler",
        source_module="flatten_controller",
        symbol="TQQQ",
    )

    await sink.record(event)

    assert seen == ["confirmed"]
