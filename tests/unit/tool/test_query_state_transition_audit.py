from __future__ import annotations

import json

from tool.query_state_transition_audit import build_state_transition_chain


def test_build_state_transition_chain_filters_state_domain_rows(tmp_path):
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
                        "stage": "cross_state_inconsistency_detected",
                        "strategy_id": "index_super_win_001",
                        "symbol": "AAPL",
                        "status": "warning",
                        "reason": "runtime_flat_conflicts_with_owned_live_order",
                        "trace_id": "trace-2",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-04-03T10:02:00-04:00",
                        "domain": "strategy",
                        "stage": "gate_blocked",
                        "strategy_id": "index_super_win_001",
                        "symbol": "AAPL",
                        "reason": "blocked",
                        "trace_id": "trace-3",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_state_transition_chain(
        audit_root=tmp_path,
        strategy_id="index_super_win_001",
        date="2026-04-03",
    )

    assert payload["summary"]["record_count"] == 2
    assert payload["summary"]["by_state_machine"]["runtime"] == 1
    assert "cross_state" not in payload["summary"]["by_state_machine"]
    assert payload["summary"]["cross_state_inconsistency_count"] == 1
    assert payload["summary"]["by_stage"]["cross_state_inconsistency_detected"] == 1


def test_build_state_transition_chain_can_exclude_inconsistency(tmp_path):
    day_dir = tmp_path / "2026-04-03"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "state-10.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": "2026-04-03T10:00:00-04:00",
                "domain": "state",
                "stage": "cross_state_inconsistency_detected",
                "strategy_id": "index_super_win_001",
                "symbol": "AAPL",
                "status": "warning",
                "reason": "runtime_flat_conflicts_with_owned_live_order",
                "trace_id": "trace-2",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = build_state_transition_chain(
        audit_root=tmp_path,
        strategy_id="index_super_win_001",
        date="2026-04-03",
        include_inconsistency=False,
    )

    assert payload["summary"]["record_count"] == 0
