from __future__ import annotations

from services.strategy_runtime_v2.parity_recorder import compare_event_streams, normalize_event


def test_normalize_event_extracts_order_ref_kind_and_runtime_transition() -> None:
    event = normalize_event(
        {
            "stage": "order_state_updated",
            "symbol": "aapl",
            "trace_id": "trace-1",
            "payload": {
                "order_ref": "iswv2:index_super_win_001:AAPL:ENTRY:TRG:abc",
                "runtime_lifecycle_before": "intent_submitted",
                "runtime_lifecycle_after": "pending_entry",
                "qty": 5,
                "side": "long",
            },
        }
    )
    assert event.symbol == "AAPL"
    assert event.order_ref_kind == "ENTRY"
    assert event.runtime_transition == "intent_submitted->pending_entry"
    assert event.side == "LONG"
    assert event.qty == 5.0


def test_compare_event_streams_detects_reason_mismatch() -> None:
    expected = [
        {"stage": "entry_gate_blocked", "symbol": "AAPL", "trace_id": "t1", "reason": "signal_inactive"},
    ]
    actual = [
        {"stage": "entry_gate_blocked", "symbol": "AAPL", "trace_id": "t1", "reason": "runtime_guard"},
    ]
    result = compare_event_streams(expected, actual)
    assert result["ok"] is False
    assert result["mismatch_count"] == 1
    assert "reason_mismatch" in result["mismatches"][0]["category"]


def test_compare_event_streams_allows_qty_tolerance() -> None:
    expected = [
        {"stage": "entry_intent_submitted", "symbol": "AAPL", "trace_id": "t1", "payload": {"qty": 10}},
    ]
    actual = [
        {"stage": "entry_intent_submitted", "symbol": "AAPL", "trace_id": "t1", "payload": {"qty": 10.5}},
    ]
    result = compare_event_streams(expected, actual, qty_tolerance=1.0)
    assert result["ok"] is True
    assert result["matched_count"] == 1
