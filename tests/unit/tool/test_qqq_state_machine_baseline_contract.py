from __future__ import annotations

import json
from pathlib import Path

import pytest

from tool.replay.qqq_index_replica_replay import validate_baseline_contract


def test_validate_baseline_contract_accepts_current_qqq_baseline() -> None:
    baseline_path = Path("docs/baselines/qqq_intraday_001_replica_baseline.json")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    report = validate_baseline_contract(baseline)
    assert report["freeze_contract_ok"] is True
    assert report["state_machine_contract_ok"] is True
    assert report["independent_state_machines"] == ["flatten", "order", "protection", "runtime"]


def test_validate_baseline_contract_rejects_missing_guardrail() -> None:
    baseline = {
        "git_head": "abc",
        "parity_fields": ["side"],
        "minimum_replay_fixtures": ["normal_entry_tp1_trailing_exit"],
        "behavior_freeze_contract": {
            "strategy_id": "qqq_intraday_001",
            "qqq_cutover_policy": "early",
            "rollback_required": True,
        },
        "state_machine_upgrade_contract": {
            "independent_state_machines": ["runtime", "order", "flatten", "protection"],
            "unified_kernel_required": True,
        },
    }
    with pytest.raises(ValueError, match="qqq_cutover_policy=last_strategy_to_cut_over"):
        validate_baseline_contract(baseline)
