from tool.build_multi_strategy_isolation_snapshot import build_snapshot


def test_build_multi_strategy_isolation_snapshot_includes_exec_audit_and_strategy_views():
    payload = build_snapshot(
        qqq_obs={
            "ready": True,
            "strategy_state": {"exec_symbol": "TQQQ", "is_traded": True, "side": "LONG"},
        },
        index_diag={
            "trader_view_by_symbol": {
                "AAPL": {
                    "final_state": "blocked",
                    "which_gate_blocked": "preview:OWNERSHIP_CONFLICT",
                    "foreign_owner_strategy_id": "qqq_intraday_001",
                    "entry_attempts": 1,
                }
            },
            "conflict_report_by_symbol": {
                "AAPL": {
                    "foreign_open_order_conflicts": 1,
                    "foreign_symbol_ownership_conflicts": 1,
                    "foreign_owner_strategy_id": "qqq_intraday_001",
                }
            },
        },
        audit_rows=[
            {
                "ts": "2026-03-14T10:00:00-04:00",
                "stage": "entry_isolation_blocked_pending_order",
                "symbol": "AAPL",
                "status": "blocked",
                "reason": "entry_isolation_blocked_pending_order",
                "payload": {"other_strategy_id": "qqq_intraday_001"},
            },
            {
                "ts": "2026-03-14T10:01:00-04:00",
                "stage": "fill_attribution_fallback_broker_order_id",
                "symbol": "TQQQ",
                "status": "resolved",
                "reason": "fill_attribution:broker_order_id",
                "payload": {"candidate_count": 2},
            },
        ],
        day="2026-03-14",
    )

    assert payload["symbols"]["TQQQ"]["qqq"]["is_traded"] is True
    assert payload["symbols"]["TQQQ"]["fill_attribution"]["stage"] == "fill_attribution_fallback_broker_order_id"
    assert payload["symbols"]["AAPL"]["entry_isolation"]["stage"] == "entry_isolation_blocked_pending_order"
    assert payload["symbols"]["AAPL"]["index_conflict"]["foreign_owner_strategy_id"] == "qqq_intraday_001"
