from __future__ import annotations

import json

from tool.report.build_state_transition_daily_summary import build_state_transition_daily_summary


def test_build_state_transition_daily_summary_counts_transitions_and_inconsistencies(tmp_path):
    day_dir = tmp_path / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "state-10.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-04-03T10:00:00-04:00",
                        "domain": "state",
                        "stage": "runtime_transition",
                        "strategy_id": "index_super_win_001",
                        "symbol": "AAPL",
                        "status": "live",
                        "reason": "trusted_fill",
                        "trace_id": "trace-1",
                        "payload": {"state_machine": "runtime"},
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-04-03T10:01:00-04:00",
                        "domain": "state",
                        "stage": "flatten_transition",
                        "strategy_id": "index_super_win_001",
                        "symbol": "AAPL",
                        "status": "confirmed",
                        "reason": "runtime_reconciler",
                        "trace_id": "trace-1",
                        "payload": {"state_machine": "flatten"},
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-04-03T10:02:00-04:00",
                        "domain": "state",
                        "stage": "cross_state_inconsistency_detected",
                        "strategy_id": "index_super_win_001",
                        "symbol": "AAPL",
                        "status": "warning",
                        "reason": "runtime_live_conflicts_with_confirmed_flatten_nonzero_broker_qty",
                        "trace_id": "trace-2",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_state_transition_daily_summary(audit_root=tmp_path, date="2026-04-03")

    assert payload["row_count"] == 3
    assert payload["cross_state_inconsistency_count"] == 1
    assert payload["state_summary"]["by_state_machine"]["flatten"] == 1
    assert "cross_state" not in payload["state_summary"]["by_state_machine"]
    assert payload["by_symbol"]["AAPL"] == 3
    assert payload["qqq_shadow_readiness"]["ready_for_shadow_acceptance"] is False
