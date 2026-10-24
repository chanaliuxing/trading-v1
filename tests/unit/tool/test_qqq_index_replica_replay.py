from __future__ import annotations

import json
from pathlib import Path

from tool.replay.qqq_index_replica_replay import build_report


def test_build_report_uses_baseline_and_compares_traces(tmp_path: Path) -> None:
    baseline = {
        "git_head": "abc123",
        "strategy_id": "qqq_intraday_001",
        "parity_fields": ["side", "qty"],
        "minimum_replay_fixtures": ["normal_entry_tp1_trailing_exit"],
        "behavior_freeze_contract": {
            "strategy_id": "qqq_intraday_001",
            "qqq_cutover_policy": "last_strategy_to_cut_over",
            "rollback_required": True,
        },
        "state_machine_upgrade_contract": {
            "independent_state_machines": ["runtime", "order", "flatten", "protection"],
            "unified_kernel_required": True,
        },
    }
    expected = {
        "events": [
            {"stage": "entry_intent_submitted", "symbol": "AAPL", "trace_id": "t1", "payload": {"qty": 5, "side": "LONG"}}
        ]
    }
    actual = {
        "events": [
            {"stage": "entry_intent_submitted", "symbol": "AAPL", "trace_id": "t1", "payload": {"qty": 5, "side": "LONG"}}
        ]
    }
    baseline_path = tmp_path / "baseline.json"
    expected_path = tmp_path / "expected.json"
    actual_path = tmp_path / "actual.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    actual_path.write_text(json.dumps(actual), encoding="utf-8")

    report = build_report(
        baseline_path=baseline_path,
        expected_trace_path=expected_path,
        actual_trace_path=actual_path,
        qty_tolerance=1.0,
    )
    assert report["baseline_git_head"] == "abc123"
    assert report["baseline_validation"]["freeze_contract_ok"] is True
    assert report["comparison"]["ok"] is True
    assert report["comparison"]["matched_count"] == 1
