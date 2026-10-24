from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.multi_day_runner import build_multi_day_run_artifacts
from scripts.ai_trader.research.incident_manager import build_incident_artifacts
from scripts.ai_trader.research.recovery_coordinator import build_recovery_coordinator_report
from scripts.ai_trader.research.operator_shift_summary import build_operator_shift_summary
from scripts.ai_trader.research.reliability_metrics import build_reliability_metrics
from scripts.ai_trader.research.ops_registry import update_ops_registry


def test_incident_recovery_shift_reliability_and_registry(tmp_path: Path) -> None:
    incidents = build_incident_artifacts(
        day_id="2026-04-05",
        batch_id="batch-a",
        intraday_batch_report={"batch_warnings": ["latency_spike"], "items": []},
        broker_readonly_probe={"status": "controlled_fail"},
        broker_reconcile_report={"clean_reconcile": False},
        kill_switch_report={"kill_switch_active": True, "actions": ["flatten_all"]},
        output_dir=tmp_path,
        state_root=tmp_path / "state",
    )
    assert Path(incidents["incident_log_jsonl"]).exists()
    assert Path(incidents["incident_index_json"]).exists()
    idx = incidents["incident_index"]
    assert idx["warning_count"] >= 1
    assert idx["critical_count"] >= 1
    assert idx["resolved_count"] >= 1

    recovery = build_recovery_coordinator_report(
        day_id="2026-04-05",
        incident_index=idx,
        trading_day_status={"current_stage": "intraday"},
        output_dir=tmp_path,
    )
    assert Path(recovery["recovery_coordinator_report_json"]).exists()
    actions = {x.get("action") for x in recovery["recovery_coordinator_report"]["actions"]}
    assert "flatten_and_pause" in actions
    assert "safe_resume_next_day" in actions

    multi = build_multi_day_run_artifacts(
        day_id="2026-04-05",
        trading_day_status={"current_stage": "archived"},
        incident_index=idx,
        recovery_coordinator_report=recovery["recovery_coordinator_report"],
        output_dir=tmp_path,
    )
    assert Path(multi["multi_day_run_plan_json"]).exists()
    assert Path(multi["multi_day_run_report_json"]).exists()
    assert len(multi["multi_day_run_report"]["samples"]["three_plus_day_run"]["cycles_run"]) >= 3

    shift = build_operator_shift_summary(
        day_id="2026-04-05",
        incident_index=idx,
        active_registry={"items": [{"candidate_id": "c1", "status": "active"}, {"candidate_id": "c2", "status": "halted"}]},
        ops_control_center={"capital_in_use": 1000.0},
        recovery_coordinator_report=recovery["recovery_coordinator_report"],
        output_dir=tmp_path,
    )
    assert Path(shift["operator_shift_summary_json"]).exists()
    assert "open_incidents_shift" in shift["operator_shift_summary"]["samples"]
    assert "clean_shift" in shift["operator_shift_summary"]["samples"]

    reliability = build_reliability_metrics(
        day_id="2026-04-05",
        multi_day_run_report=multi["multi_day_run_report"],
        incident_index=idx,
        broker_readonly_probe={"status": "controlled_fail"},
        broker_reconcile_report={"clean_reconcile": False},
        recovery_coordinator_report=recovery["recovery_coordinator_report"],
        live_ops_summary={"halted_candidates": ["c2"], "flattened_candidates": ["c2"]},
        output_dir=tmp_path,
    )
    assert Path(reliability["reliability_metrics_json"]).exists()
    assert reliability["reliability_metrics"]["status"] == "degraded"

    registry = update_ops_registry(
        day_id="2026-04-05",
        system_mode="small_live",
        trading_day_status={"current_stage": "archived"},
        launch_status={"verdict": "hold_live"},
        ops_control_center={"active_batches": ["batch-a"], "active_candidates": ["c1"]},
        incident_index=idx,
        reliability_metrics=reliability["reliability_metrics"],
        state_root=tmp_path / "state",
    )
    assert Path(registry["ops_registry_json"]).exists()
    payload = json.loads(Path(registry["ops_registry_json"]).read_text(encoding="utf-8"))
    assert payload["open_incidents"] >= 1
    assert payload["reliability_status"] == "degraded"

