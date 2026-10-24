from __future__ import annotations

import json
from pathlib import Path

from services.production.ib_restart_drill_artifact import build_ib_restart_drill_artifact


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_ib_restart_drill_artifact_go_from_real_audit_shape(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/runtime.yaml").write_text("mode: paper\n", encoding="utf-8")
    audit = tmp_path / "runtime/audit/2026-05-04"
    _write_jsonl(
        audit / "system-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:51:00-04:00",
                "component": "market_data_service",
                "stage": "ib_disconnected",
                "status": "disconnected",
            },
            {
                "ts": "2026-05-04T10:52:00-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "recovering",
                "payload": {"event_type": "system_recovering", "metadata": {"platform_recovery_ready": False}},
            },
            {
                "ts": "2026-05-04T10:53:00-04:00",
                "component": "market_data_service",
                "stage": "ib_connected",
                "status": "connected",
            },
            {
                "ts": "2026-05-04T10:54:00-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "payload": {
                    "event_type": "system_resumed",
                    "metadata": {
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True, "blockers": []},
                    },
                },
            },
        ],
    )
    _write_jsonl(
        audit / "recon-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:53:30-04:00",
                "component": "runtime_reconciler",
                "stage": "position_audit_snapshot",
                "payload": {
                    "ghosts": 0,
                    "orphans_unresolved": 0,
                    "mismatches": 0,
                    "position_mismatches": 0,
                    "active_quarantine_count": 0,
                },
            }
        ],
    )

    artifact = build_ib_restart_drill_artifact(
        repo_root=tmp_path,
        day="2026-05-04",
        start_ts="2026-05-04T09:30:00-04:00",
    )

    assert artifact["go_no_go"] == "go"
    assert artifact["source_pid"] > 0
    assert artifact["process_start_time"] == "2026-05-04T09:30:00-04:00"
    assert len(artifact["config_hash"]) == 64
    assert "git_head" in artifact
    assert artifact["blockers"] == []
    assert [item["kind"] for item in artifact["timeline"]] == [
        "ib_disconnected",
        "system_recovering",
        "ib_reconnected",
        "runtime_reconcile_clean",
        "system_resumed",
    ]
    assert (tmp_path / "runtime/readiness/drills/paper_ib_restart_latest.json").exists()


def test_ib_restart_drill_artifact_blocks_missing_resumed(tmp_path: Path) -> None:
    audit = tmp_path / "runtime/audit/2026-05-04"
    _write_jsonl(
        audit / "system-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:51:00-04:00",
                "component": "market_data_service",
                "stage": "ib_disconnected",
            },
            {
                "ts": "2026-05-04T10:53:00-04:00",
                "component": "market_data_service",
                "stage": "ib_connected",
            },
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-04")

    assert artifact["go_no_go"] == "no_go"
    assert "system_resumed_after_reconnect_missing" in artifact["blockers"]


def test_ib_restart_drill_artifact_without_disconnect_does_not_report_stable_window_noise(tmp_path: Path) -> None:
    audit = tmp_path / "runtime/audit/2026-05-04"
    _write_jsonl(
        audit / "system-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:53:00-04:00",
                "component": "market_data_service",
                "stage": "service_started",
                "status": "started",
            },
            {
                "ts": "2026-05-04T10:54:00-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "payload": {
                    "event_type": "system_resumed",
                    "metadata": {
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True, "blockers": []},
                    },
                },
            },
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-04")

    assert artifact["go_no_go"] == "no_go"
    assert "ib_disconnect_event_missing" in artifact["blockers"]
    assert "runtime_reconcile_clean_missing" in artifact["blockers"]
    assert "post_resume_stable_window_not_met" not in artifact["blockers"]


def test_ib_restart_drill_artifact_accepts_current_process_recovery_without_disconnect(tmp_path: Path) -> None:
    audit = tmp_path / "runtime/audit/2026-05-04"
    _write_jsonl(
        audit / "system-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:53:00-04:00",
                "component": "market_data_service",
                "stage": "service_started",
                "status": "started",
            },
            {
                "ts": "2026-05-04T10:53:30-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "recovering",
                "payload": {"event_type": "system_recovering"},
            },
            {
                "ts": "2026-05-04T10:54:00-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "payload": {
                    "event_type": "system_resumed",
                    "metadata": {
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True, "blockers": []},
                    },
                },
            },
        ],
    )
    _write_jsonl(
        audit / "recon-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:53:45-04:00",
                "component": "runtime_reconciler",
                "stage": "position_audit_snapshot",
                "payload": {
                    "ghosts": 0,
                    "orphans_unresolved": 0,
                    "mismatches": 0,
                    "position_mismatches": 0,
                    "active_quarantine_count": 0,
                },
            }
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-04")

    assert artifact["go_no_go"] == "go"
    assert artifact["proof_mode"] == "process_recovery_without_disconnect"
    assert artifact["blockers"] == []


def test_ib_restart_drill_artifact_accepts_startup_broker_truth_without_recon_snapshot(tmp_path: Path) -> None:
    audit = tmp_path / "runtime/audit/2026-05-04"
    _write_jsonl(
        audit / "system-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:53:00-04:00",
                "component": "market_data_service",
                "stage": "service_started",
                "status": "started",
            },
            {
                "ts": "2026-05-04T10:53:30-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "recovering",
                "payload": {"event_type": "system_recovering"},
            },
            {
                "ts": "2026-05-04T10:54:00-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "reason": "startup_broker_truth_ready",
                "payload": {
                    "event_type": "system_resumed",
                    "metadata": {
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                        "broker_connected": True,
                        "broker_truth_complete": True,
                        "order_truth_clean": True,
                        "position_ledger_clean": True,
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True, "blockers": []},
                    },
                },
            },
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-04")

    assert artifact["go_no_go"] == "go"
    assert artifact["proof_mode"] == "process_recovery_without_disconnect"
    assert artifact["blockers"] == []
    assert "startup_broker_truth_clean" in [item["kind"] for item in artifact["timeline"]]
    assert artifact["counts"]["startup_broker_truth_clean_events"] == 1


def test_ib_restart_drill_artifact_soft_resume_heartbeat_does_not_reset_stable_window(tmp_path: Path) -> None:
    audit = tmp_path / "runtime/audit/2026-05-04"
    _write_jsonl(
        audit / "system-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:51:00-04:00",
                "component": "market_data_service",
                "stage": "ib_disconnected",
                "status": "disconnected",
                "reason": "operator_paper_ib_restart_drill",
            },
            {
                "ts": "2026-05-04T10:52:00-04:00",
                "component": "market_data_service",
                "stage": "ib_connected",
                "status": "connected",
                "reason": "operator_paper_ib_restart_drill",
            },
            {
                "ts": "2026-05-04T10:52:01-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "recovering",
                "reason": "operator_paper_ib_restart_drill_reconnected",
                "payload": {"event_type": "system_recovering"},
            },
            {
                "ts": "2026-05-04T10:52:02-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "reason": "operator_paper_ib_restart_drill_verified",
                "payload": {
                    "event_type": "system_resumed",
                    "source": "run_system.operator_drill",
                    "metadata": {
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True, "blockers": []},
                    },
                },
            },
            {
                "ts": "2026-05-04T10:54:00-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "reason": "production_state_soft_no_go",
                "payload": {
                    "event_type": "system_resumed",
                    "source": "production_state_emitter",
                    "metadata": {
                        "drill_mode": "operator_paper",
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True, "blockers": []},
                    },
                },
            },
        ],
    )
    _write_jsonl(
        audit / "recon-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:53:00-04:00",
                "component": "runtime_reconciler",
                "stage": "position_audit_snapshot",
                "payload": {
                    "ghosts": 0,
                    "orphans_unresolved": 0,
                    "mismatches": 0,
                    "position_mismatches": 0,
                    "active_quarantine_count": 0,
                },
            }
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-04")

    assert artifact["go_no_go"] == "go"
    assert artifact["blockers"] == []
    resumed = [item for item in artifact["timeline"] if item["kind"] == "system_resumed"]
    assert resumed[-1]["reason"] == "operator_paper_ib_restart_drill_verified"


def test_ib_restart_drill_soft_resume_heartbeats_do_not_trip_day_flapping(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IB_RESTART_DRILL_DAY_MAX_RESUMED_EVENTS", "2")
    audit = tmp_path / "runtime/audit/2026-05-04"
    rows = [
        {
            "ts": "2026-05-04T10:51:00-04:00",
            "component": "market_data_service",
            "stage": "ib_disconnected",
            "status": "disconnected",
            "reason": "operator_paper_ib_restart_drill",
        },
        {
            "ts": "2026-05-04T10:52:00-04:00",
            "component": "market_data_service",
            "stage": "ib_connected",
            "status": "connected",
            "reason": "operator_paper_ib_restart_drill",
        },
        {
            "ts": "2026-05-04T10:52:01-04:00",
            "component": "system_health_controller",
            "stage": "health_transition",
            "status": "recovering",
            "reason": "operator_paper_ib_restart_drill_reconnected",
            "payload": {"event_type": "system_recovering"},
        },
        {
            "ts": "2026-05-04T10:52:02-04:00",
            "component": "system_health_controller",
            "stage": "health_transition",
            "status": "running",
            "reason": "operator_paper_ib_restart_drill_verified",
            "payload": {
                "event_type": "system_resumed",
                "source": "run_system.operator_drill",
                "metadata": {
                    "ghosts": 0,
                    "unresolved_orphans": 0,
                    "mismatches": 0,
                    "position_mismatches": 0,
                    "active_quarantine_count": 0,
                    "platform_recovery_ready": True,
                    "recovery_readiness": {"ready": True, "blockers": []},
                },
            },
        },
    ]
    for idx in range(5):
        rows.append(
            {
                "ts": f"2026-05-04T10:53:0{idx}-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "reason": "production_state_soft_no_go",
                "payload": {
                    "event_type": "system_resumed",
                    "source": "production_state_emitter",
                    "metadata": {"platform_recovery_ready": True, "recovery_readiness": {"ready": True}},
                },
            }
        )
    _write_jsonl(audit / "system-10.jsonl", rows)
    _write_jsonl(
        audit / "recon-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:53:00-04:00",
                "component": "runtime_reconciler",
                "stage": "position_audit_snapshot",
                "payload": {
                    "ghosts": 0,
                    "orphans_unresolved": 0,
                    "mismatches": 0,
                    "position_mismatches": 0,
                    "active_quarantine_count": 0,
                },
            }
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-04")

    assert artifact["go_no_go"] == "go"
    assert "system_day_resume_flapping_threshold_exceeded" not in artifact["blockers"]
    assert artifact["day_counts"]["resumed_events"] == 6
    assert artifact["day_counts"]["hard_resumed_events"] == 1


def test_ib_restart_drill_operator_simulated_disconnects_do_not_trip_day_ib_flapping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IB_RESTART_DRILL_DAY_MAX_DISCONNECT_EVENTS", "2")
    monkeypatch.setenv("IB_RESTART_DRILL_DAY_MAX_RECONNECT_EVENTS", "2")
    audit = tmp_path / "runtime/audit/2026-05-04"
    rows = []
    for idx in range(5):
        rows.extend(
            [
                {
                    "ts": f"2026-05-04T10:5{idx}:00-04:00",
                    "component": "market_data_service",
                    "stage": "ib_disconnected",
                    "status": "disconnected",
                    "reason": "operator_paper_ib_restart_drill",
                    "payload": {"simulated": True, "drill_mode": "operator_paper"},
                },
                {
                    "ts": f"2026-05-04T10:5{idx}:01-04:00",
                    "component": "market_data_service",
                    "stage": "ib_connected",
                    "status": "connected",
                    "reason": "operator_paper_ib_restart_drill_reconnected",
                    "payload": {"simulated": True, "drill_mode": "operator_paper"},
                },
            ]
        )
    rows.extend(
        [
            {
                "ts": "2026-05-04T10:54:02-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "recovering",
                "reason": "operator_paper_ib_restart_drill_reconnected",
                "payload": {"event_type": "system_recovering"},
            },
            {
                "ts": "2026-05-04T10:54:03-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "reason": "operator_paper_ib_restart_drill_verified",
                "payload": {
                    "event_type": "system_resumed",
                    "source": "run_system.operator_drill",
                    "metadata": {
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True, "blockers": []},
                    },
                },
            },
        ]
    )
    _write_jsonl(audit / "system-10.jsonl", rows)
    _write_jsonl(
        audit / "recon-10.jsonl",
        [
            {
                "ts": "2026-05-04T10:54:02-04:00",
                "component": "runtime_reconciler",
                "stage": "position_audit_snapshot",
                "payload": {
                    "ghosts": 0,
                    "orphans_unresolved": 0,
                    "mismatches": 0,
                    "position_mismatches": 0,
                    "active_quarantine_count": 0,
                },
            }
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-04")

    assert artifact["go_no_go"] == "go"
    assert "ib_day_disconnect_flapping_threshold_exceeded" not in artifact["blockers"]
    assert "ib_day_reconnect_flapping_threshold_exceeded" not in artifact["blockers"]
    assert artifact["day_counts"]["disconnect_events"] == 5
    assert artifact["day_counts"]["hard_disconnect_events"] == 0
    assert artifact["day_counts"]["hard_reconnect_events"] == 0
