from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_replica_final_enablement_checklist import build_replica_final_enablement_checklist


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_replica_final_enablement_checklist_green(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "strategies:\n"
        "  - id: index_super_win_001\n"
        "    enabled: false\n"
        "    module: strategies.index_super_win_replica_strategy\n"
        "    class: IndexSuperWinReplicaStrategy\n",
        encoding="utf-8",
    )
    preflight_path = tmp_path / "preflight.json"
    summary_path = tmp_path / "summary.json"
    batch_path = tmp_path / "batch.json"
    isolation_summary_path = tmp_path / "isolation_summary.json"
    trace_smoke_path = tmp_path / "trace_smoke.json"
    session_bundle_path = tmp_path / "session_bundle.json"

    _write_json(preflight_path, {"ready_for_enablement": True})
    _write_json(
        summary_path,
        {
            "has_any_ready_day": True,
            "pipeline_status": "batch_completed",
            "ready_days": ["2026-03-24", "2026-03-25", "2026-03-26"],
            "needs_more_real_samples": False,
        },
    )
    _write_json(
        batch_path,
        {
            "green_days": 3,
            "enablement_attestation_green_days": 3,
            "rows": [
                {"day": "2026-03-24", "enablement_attestation_green": True},
                {"day": "2026-03-25", "enablement_attestation_green": True},
                {"day": "2026-03-26", "enablement_attestation_green": True},
            ],
        },
    )
    _write_json(
        isolation_summary_path,
        {
            "high_value_event_count": 0,
        },
    )
    _write_json(
        trace_smoke_path,
        {
            "ready_for_trace_parity_capture": True,
            "observed": {"row_count": 12, "trace_count": 3, "decision_symbol_count": 1},
        },
    )
    _write_json(session_bundle_path, {"ready_for_enablement": True, "errors": []})

    report = build_replica_final_enablement_checklist(
        config_path=config_path,
        strategy_id="index_super_win_001",
        expected_replica_module="strategies.index_super_win_replica_strategy",
        expected_replica_class="IndexSuperWinReplicaStrategy",
        preflight_report_path=preflight_path,
        real_session_summary_path=summary_path,
        batch_report_path=batch_path,
        isolation_daily_summary_path=isolation_summary_path,
        trace_smoke_gate_path=trace_smoke_path,
        session_bundle_report_path=session_bundle_path,
        max_high_value_isolation_events=0,
        required_green_days=3,
    )

    assert report["ready_to_enable"] is True
    assert report["blockers"] == []


def test_build_replica_final_enablement_checklist_reports_blockers(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "strategies:\n"
        "  - id: index_super_win_001\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    preflight_path = tmp_path / "preflight.json"
    summary_path = tmp_path / "summary.json"
    batch_path = tmp_path / "batch.json"
    isolation_summary_path = tmp_path / "isolation_summary.json"
    trace_smoke_path = tmp_path / "trace_smoke.json"
    session_bundle_path = tmp_path / "session_bundle.json"

    _write_json(preflight_path, {"ready_for_enablement": False})
    _write_json(
        summary_path,
        {
            "has_any_ready_day": False,
            "pipeline_status": "readiness_only",
            "ready_days": [],
            "needs_more_real_samples": True,
        },
    )
    _write_json(
        batch_path,
        {
            "green_days": 1,
            "enablement_attestation_green_days": 0,
            "rows": [
                {"day": "2026-03-24", "status": "completed", "overall_green": False, "enablement_attestation_green": False},
            ],
        },
    )
    _write_json(
        isolation_summary_path,
        {
            "high_value_event_count": 3,
        },
    )
    _write_json(
        trace_smoke_path,
        {
            "ready_for_trace_parity_capture": False,
            "observed": {"row_count": 24, "trace_count": 0, "decision_symbol_count": 0},
        },
    )
    _write_json(session_bundle_path, {"ready_for_enablement": False, "errors": ["overlap"]})

    report = build_replica_final_enablement_checklist(
        config_path=config_path,
        strategy_id="index_super_win_001",
        expected_replica_module="strategies.index_super_win_replica_strategy",
        expected_replica_class="IndexSuperWinReplicaStrategy",
        preflight_report_path=preflight_path,
        real_session_summary_path=summary_path,
        batch_report_path=batch_path,
        isolation_daily_summary_path=isolation_summary_path,
        trace_smoke_gate_path=trace_smoke_path,
        session_bundle_report_path=session_bundle_path,
        max_high_value_isolation_events=0,
        required_green_days=3,
    )

    assert report["ready_to_enable"] is False
    assert "strategy_not_disabled_in_config" in report["blockers"]
    assert "strategy_not_pointing_to_replica_module_class" in report["blockers"]
    assert "preflight_not_green" in report["blockers"]
    assert "no_ready_real_session_day" in report["blockers"]
    assert "real_session_pipeline_not_completed" in report["blockers"]
    assert "insufficient_green_proof_days" in report["blockers"]
    assert "insufficient_green_attestation_days" in report["blockers"]
    assert "high_value_isolation_events_exceeded" in report["blockers"]
    assert "trace_smoke_gate_not_ready" in report["blockers"]
    assert "session_bundle_not_green" in report["blockers"]
