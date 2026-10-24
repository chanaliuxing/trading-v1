from tool.build_multi_strategy_isolation_daily_summary import build_daily_summary


def test_build_daily_summary_only_counts_high_value_events():
    payload = build_daily_summary(
        day="2026-03-14",
        audit_rows=[
            {
                "ts": "2026-03-14T10:00:00-04:00",
                "stage": "entry_isolation_blocked_pending_order",
                "symbol": "AAPL",
                "reason": "entry_isolation_blocked_pending_order",
                "trace_id": "t1",
            },
            {
                "ts": "2026-03-14T10:01:00-04:00",
                "stage": "entry_isolation_lock_acquired",
                "symbol": "AAPL",
                "reason": "symbol_advisory_lock_acquired",
                "trace_id": "t1",
            },
            {
                "ts": "2026-03-14T10:02:00-04:00",
                "stage": "fill_attribution_fallback_broker_order_id",
                "symbol": "TQQQ",
                "reason": "fill_attribution:broker_order_id",
                "trace_id": "t2",
            },
        ],
    )

    assert payload["high_value_event_count"] == 2
    assert payload["high_value_stage_counter"]["entry_isolation_blocked_pending_order"] == 1
    assert payload["high_value_stage_counter"]["fill_attribution_fallback_broker_order_id"] == 1
    assert "entry_isolation_lock_acquired" not in payload["high_value_stage_counter"]
