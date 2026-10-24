from __future__ import annotations

from pathlib import Path

from scripts.ai_trader.research.incident_manager import build_incident_artifacts
from scripts.ai_trader.research.ops_hardening_acceptance import (
    build_acceptance_decision,
    build_artifact_index,
    build_consistency_report,
    build_postflight_check,
)
from scripts.ai_trader.research.preflight_check import build_preflight_check
from scripts.ai_trader.research.recovery_coordinator import build_recovery_coordinator_report


def test_success_cycle_artifact_index_and_consistency_check(tmp_path: Path) -> None:
    for name in [
        "preflight_check.json",
        "launch_verdict.json",
        "evidence_integrity_report.json",
        "incident_index.json",
        "recovery_coordinator_report.json",
        "operator_shift_summary.json",
        "reliability_metrics.json",
    ]:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "ops_registry.json").write_text("{}", encoding="utf-8")

    index_bundle = build_artifact_index(
        cycle_id="2026-04-08",
        run_id="2026-04-08",
        cycle_dir=tmp_path,
        state_root=state_root,
        required_artifacts={
            "preflight_check": tmp_path / "preflight_check.json",
            "launch_verdict": tmp_path / "launch_verdict.json",
            "evidence_integrity_report": tmp_path / "evidence_integrity_report.json",
            "incident_index": tmp_path / "incident_index.json",
            "recovery_coordinator_report": tmp_path / "recovery_coordinator_report.json",
            "operator_shift_summary": tmp_path / "operator_shift_summary.json",
            "reliability_metrics": tmp_path / "reliability_metrics.json",
            "ops_registry": state_root / "ops_registry.json",
        },
    )
    assert index_bundle["artifact_index"]["overall_status"] == "ok"

    consistency_bundle = build_consistency_report(
        cycle_id="2026-04-08",
        run_id="2026-04-08",
        incident_index={"day_id": "2026-04-08", "run_id": "2026-04-08"},
        recovery_report={"day_id": "2026-04-08", "run_id": "2026-04-08"},
        shift_summary={"day_id": "2026-04-08", "run_id": "2026-04-08"},
        reliability_metrics={"day_id": "2026-04-08", "run_id": "2026-04-08"},
        ops_registry={"last_cycle_id": "2026-04-08", "run_id": "2026-04-08"},
        artifact_index=index_bundle["artifact_index"],
        output_dir=tmp_path,
    )
    assert consistency_bundle["consistency_report"]["overall_status"] == "pass"


def test_failed_cycle_index_and_consistency_detects_anomalies(tmp_path: Path) -> None:
    index_bundle = build_artifact_index(
        cycle_id="2026-04-08",
        run_id="2026-04-08",
        cycle_dir=tmp_path,
        state_root=tmp_path / "state",
        required_artifacts={"missing_required": tmp_path / "missing.json"},
    )
    assert index_bundle["artifact_index"]["overall_status"] == "missing_artifacts"

    consistency_bundle = build_consistency_report(
        cycle_id="2026-04-08",
        run_id="2026-04-08",
        incident_index={"day_id": "2026-04-07", "run_id": "2026-04-08"},
        recovery_report={"day_id": "2026-04-08", "run_id": "2026-04-07"},
        shift_summary={"day_id": "2026-04-08", "run_id": "2026-04-08"},
        reliability_metrics={"day_id": "2026-04-08", "run_id": "2026-04-08"},
        ops_registry={"last_cycle_id": "2026-04-07", "run_id": "2026-04-08"},
        artifact_index=index_bundle["artifact_index"],
        output_dir=tmp_path,
    )
    report = consistency_bundle["consistency_report"]
    assert report["overall_status"] == "fail"
    assert report["mismatches"]


def test_recovery_rerun_idempotent(tmp_path: Path) -> None:
    first = build_recovery_coordinator_report(
        day_id="2026-04-08",
        run_id="2026-04-08",
        incident_index={"critical_count": 1, "open_count": 1},
        trading_day_status={"current_stage": "post_close"},
        output_dir=tmp_path,
    )
    second = build_recovery_coordinator_report(
        day_id="2026-04-08",
        run_id="2026-04-08",
        incident_index={"critical_count": 1, "open_count": 1},
        trading_day_status={"current_stage": "post_close"},
        output_dir=tmp_path,
    )
    assert first["recovery_coordinator_report"]["actions"] == second["recovery_coordinator_report"]["actions"]
    assert second["recovery_coordinator_report"].get("idempotent_reuse") is True


def test_incident_close_rerun_idempotent(tmp_path: Path) -> None:
    first = build_incident_artifacts(
        day_id="2026-04-08",
        run_id="2026-04-08",
        batch_id="batch-1",
        intraday_batch_report={"batch_warnings": ["latency_warning"]},
        broker_readonly_probe={"status": "success"},
        broker_reconcile_report={"clean_reconcile": True},
        kill_switch_report={"kill_switch_active": False, "actions": []},
        output_dir=tmp_path,
        state_root=tmp_path / "state",
    )
    second = build_incident_artifacts(
        day_id="2026-04-08",
        run_id="2026-04-08",
        batch_id="batch-1",
        intraday_batch_report={"batch_warnings": ["latency_warning"]},
        broker_readonly_probe={"status": "success"},
        broker_reconcile_report={"clean_reconcile": True},
        kill_switch_report={"kill_switch_active": False, "actions": []},
        output_dir=tmp_path,
        state_root=tmp_path / "state",
    )
    assert first["incident_index"]["total_count"] == second["incident_index"]["total_count"]
    assert second["incident_index"]["resolved_count"] >= 1


def test_preflight_intercept_blocks_when_controls_or_artifacts_fail(tmp_path: Path) -> None:
    blocked = build_preflight_check(
        cycle_id="2026-04-08",
        operator_controls={"global_pause": True, "live_enabled": False},
        capital_governor_report={"summary": {"approved": 0, "rejected": 1}},
        session_control_report={"summary": {"blocked": 2, "allowed": 0}},
        admission_gate={"summary": {"approved": 0, "rejected": 1}},
        health_monitor_report={"summary": {"critical": 1, "warning": 0}},
        active_registry={"summary": {"active": 0}},
        candidate_lifecycle_index={"candidates": []},
        live_feedback_index={},
        required_artifacts=[str(tmp_path / "missing-required.json")],
    )
    assert blocked["overall_status"] == "blocked"
    assert blocked["blocking_issues"]


def test_postflight_and_acceptance_outputs(tmp_path: Path) -> None:
    postflight = build_postflight_check(
        cycle_id="2026-04-08",
        run_id="2026-04-08",
        preflight_check={"overall_status": "ready"},
        consistency_report={"overall_status": "pass"},
        recovery_report={"actions": [{"action": "continue_normal_schedule", "applied": True}]},
        incident_index={"critical_count": 0, "open_count": 0},
        reliability_metrics={"status": "healthy"},
        output_dir=tmp_path,
    )
    acceptance = build_acceptance_decision(
        cycle_id="2026-04-08",
        run_id="2026-04-08",
        preflight_check={"overall_status": "ready"},
        postflight_check=postflight["postflight_check"],
        reliability_metrics={"status": "healthy"},
        incident_index={"open_count": 0},
        output_dir=tmp_path,
    )
    assert postflight["postflight_check"]["overall_status"] == "ready"
    assert acceptance["acceptance_decision"]["accepted"] is True
