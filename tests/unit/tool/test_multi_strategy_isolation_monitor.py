from tool.multi_strategy_isolation_monitor import build_alerts_payload, collect_high_value_alerts


def test_collect_high_value_alerts_deduplicates_and_keeps_latest():
    rows = [
        {
            "ts": "2026-03-14T10:00:00-04:00",
            "stage": "entry_isolation_blocked_pending_order",
            "severity": "warning",
            "symbol": "AAPL",
            "reason": "entry_isolation_blocked_pending_order",
            "trace_id": "t1",
        },
        {
            "ts": "2026-03-14T10:01:00-04:00",
            "stage": "entry_isolation_blocked_pending_order",
            "severity": "warning",
            "symbol": "AAPL",
            "reason": "entry_isolation_blocked_pending_order",
            "trace_id": "t1",
        },
        {
            "ts": "2026-03-14T10:02:00-04:00",
            "stage": "fill_attribution_ambiguous",
            "severity": "warning",
            "symbol": "TQQQ",
            "reason": "fill_attribution:broker_order_id",
            "trace_id": "t2",
        },
    ]

    alerts = collect_high_value_alerts(rows, limit=10)
    assert len(alerts) == 2
    assert alerts[0]["stage"] == "fill_attribution_ambiguous"
    assert alerts[1]["ts"] == "2026-03-14T10:01:00-04:00"


def test_build_alerts_payload_counts_by_stage():
    payload = build_alerts_payload(
        day="2026-03-14",
        rows=[
            {
                "ts": "2026-03-14T10:00:00-04:00",
                "stage": "entry_isolation_blocked_pending_order",
                "severity": "warning",
                "symbol": "AAPL",
                "reason": "entry_isolation_blocked_pending_order",
                "trace_id": "t1",
            },
            {
                "ts": "2026-03-14T10:02:00-04:00",
                "stage": "fill_attribution_ambiguous",
                "severity": "warning",
                "symbol": "TQQQ",
                "reason": "fill_attribution:broker_order_id",
                "trace_id": "t2",
            },
        ],
        limit=10,
    )

    assert payload["alert_count"] == 2
    assert payload["stage_counter"]["entry_isolation_blocked_pending_order"] == 1
    assert payload["stage_counter"]["fill_attribution_ambiguous"] == 1
