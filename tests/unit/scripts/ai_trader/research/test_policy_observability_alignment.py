from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.ai_trader.research.human_ops_summary import build_human_ops_summary
from scripts.ai_trader.research.launch_verdict import build_launch_verdict
from scripts.ai_trader.research.readiness_dashboard import build_readiness_dashboard


def _preflight() -> dict:
    return {
        "cycle_id": "c-align",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": "ready",
        "checks": [
            {"name": "operator_controls", "status": "ready"},
            {"name": "session_control", "status": "ready"},
            {"name": "capital_governor", "status": "ready"},
            {"name": "required_artifacts", "status": "ready"},
        ],
        "blocking_issues": [],
    }


def _write_summary(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "known_count": 2,
                    "legacy_count": 1,
                    "unknown_count": 1,
                    "degraded_count": 2,
                    "digest_changed_without_version_bump_count": 1,
                },
                "recent_hits": {
                    "legacy": [{"cycle_id": "c-001"}],
                    "unknown": [{"cycle_id": "c-004"}, {"cycle_id": "c-002"}],
                    "degraded": [{"cycle_id": "c-004"}],
                    "digest_changed_without_version_bump": [{"cycle_id": "c-003"}],
                },
                "current_cycle": {
                    "cycle_id": "c-004",
                    "policy_state": "unknown",
                    "current_policy": {"policy_id": "preflight-policy", "version": "v9", "digest": "digest-v9"},
                    "history_summary": {"history_count": 4},
                    "anomaly_count": 1,
                    "legacy_hint_count": 1,
                    "advisory_count": 1,
                    "anomalies": ["digest_changed_without_version_bump"],
                    "legacy_hints": ["legacy_history_reports_missing_fields:1"],
                    "advisories": ["current_policy_not_found_in_history"],
                    "degraded": True,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _base_inputs() -> dict:
    return {
        "admission_gate": {"summary": {"approved": 1, "rejected": 0}, "items": [{"candidate_id": "cand-1", "admission_status": "approved"}]},
        "health_monitor_report": {"summary": {"critical": 0, "warning": 0}},
        "active_registry": {"summary": {"active": 1, "inactive": 0, "halted": 0}, "items": [{"candidate_id": "cand-1"}]},
    }


def test_three_views_share_same_observability_shape_and_values(tmp_path: Path) -> None:
    summary_path = tmp_path / "state" / "policy_compatibility_summary.json"
    inventory_path = tmp_path / "state" / "policy_observability_consumers.json"
    _write_summary(summary_path)
    inventory_path.write_text(
        json.dumps(
            {
                "summary": {"runtime_legacy_consumers": 1},
                "entries": [
                    {
                        "file_path": "scripts/a.py",
                        "reference_type": "runtime",
                        "legacy_fields": ["policy_compatibility"],
                        "is_legacy": True,
                        "migration_priority": "high",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    preflight = _preflight()
    inputs = _base_inputs()

    readiness = build_readiness_dashboard(
        cycle_id="c-align",
        preflight_check=preflight,
        admission_gate=inputs["admission_gate"],
        health_monitor_report=inputs["health_monitor_report"],
        active_registry=inputs["active_registry"],
        policy_compatibility_summary_path=summary_path,
        policy_observability_consumers_path=inventory_path,
    )
    verdict = build_launch_verdict(
        cycle_id="c-align",
        preflight_check=preflight,
        launch_checklist={"steps": []},
        readiness_dashboard=readiness,
        admission_gate=inputs["admission_gate"],
        policy_compatibility_summary_path=summary_path,
        policy_observability_consumers_path=inventory_path,
    )
    human = build_human_ops_summary(
        day_id="c-align",
        trading_day_status={"current_stage": "archived"},
        ops_control_center={"live_enabled": False, "system_mode": "paper", "active_batches": [], "active_candidates": []},
        end_of_day_review={"orders_summary": {"total_orders": 0}, "fills_summary": {"total_fills": 0}, "candidate_outcomes": []},
        weekly_candidate_review={"promote": [], "demote": [], "retire": [], "watchlist": []},
        review_plan_patch={"actions": []},
        output_dir=tmp_path,
        policy_compatibility_summary_path=summary_path,
        policy_observability_consumers_path=inventory_path,
    )["human_ops_summary"]

    ro = readiness["policy_compatibility_observability"]
    vo = verdict["policy_compatibility_observability"]
    ho = human["policy_compatibility_observability"]
    assert ro == vo == ho
    assert set(ro.keys()) >= {"status", "source_path", "counts", "current_cycle", "recent_examples"}
    assert ro["counts"]["digest_changed_without_version_bump"] == 1
    assert ro["current_cycle"]["policy_state"] == "unknown"
    assert ro["migration_notice"]["status"] == "active"
    assert ro["migration_notice"]["throttled"] is True
    assert ro["migration_notice"]["throttle_key"]
    assert ro["notice_lifecycle"]["first_seen"]
    assert ro["notice_lifecycle"]["last_seen"]
    assert "active" in ro["notice_lifecycle"]
    assert "silenced" in ro["notice_lifecycle"]
    assert "lifecycle_status" in ro["notice_lifecycle"]
    assert "silenced_until" in ro["notice_lifecycle"]
    assert ro["migration_progress"]["status"] == "ok"
    assert ro["migration_progress"]["runtime_legacy_consumer_count"] == 1
    assert ro["migration_progress"]["total_runtime_consumers"] == 0
    assert ro["migration_progress"]["progress_ratio"] == 1.0

    # old field compatibility kept for existing consumers
    assert readiness["policy_compatibility"]["policy_state"] == "unknown"
    assert verdict["policy_compatibility"]["history_count"] == 4


def test_three_views_share_same_fallback_for_missing_and_corrupt_summary(tmp_path: Path) -> None:
    preflight = _preflight()
    inputs = _base_inputs()
    missing_path = tmp_path / "state" / "missing.json"
    inventory_path = tmp_path / "state" / "policy_observability_consumers.json"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(json.dumps({"summary": {"runtime_legacy_consumers": 0}, "entries": []}), encoding="utf-8")

    readiness_missing = build_readiness_dashboard(
        cycle_id="c-miss",
        preflight_check=preflight,
        admission_gate=inputs["admission_gate"],
        health_monitor_report=inputs["health_monitor_report"],
        active_registry=inputs["active_registry"],
        policy_compatibility_summary_path=missing_path,
        policy_observability_consumers_path=inventory_path,
    )
    verdict_missing = build_launch_verdict(
        cycle_id="c-miss",
        preflight_check=preflight,
        launch_checklist={"steps": []},
        readiness_dashboard=readiness_missing,
        admission_gate=inputs["admission_gate"],
        policy_compatibility_summary_path=missing_path,
        policy_observability_consumers_path=inventory_path,
    )
    human_missing = build_human_ops_summary(
        day_id="c-miss",
        trading_day_status={"current_stage": "archived"},
        ops_control_center={"live_enabled": False, "system_mode": "paper", "active_batches": [], "active_candidates": []},
        end_of_day_review={"orders_summary": {"total_orders": 0}, "fills_summary": {"total_fills": 0}, "candidate_outcomes": []},
        weekly_candidate_review={"promote": [], "demote": [], "retire": [], "watchlist": []},
        review_plan_patch={"actions": []},
        output_dir=tmp_path,
        policy_compatibility_summary_path=missing_path,
        policy_observability_consumers_path=inventory_path,
    )["human_ops_summary"]
    assert readiness_missing["policy_compatibility_observability"] == verdict_missing["policy_compatibility_observability"] == human_missing["policy_compatibility_observability"]
    assert readiness_missing["policy_compatibility_observability"]["status"] == "unavailable"
    assert readiness_missing["policy_compatibility_observability"]["migration_notice"]["status"] == "none"
    assert readiness_missing["policy_compatibility_observability"]["migration_progress"]["status"] == "ok"
    assert readiness_missing["policy_compatibility_observability"].get("notice_lifecycle")

    corrupt_path = tmp_path / "state" / "corrupt.json"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("{bad-json", encoding="utf-8")
    readiness_corrupt = build_readiness_dashboard(
        cycle_id="c-corrupt",
        preflight_check=preflight,
        admission_gate=inputs["admission_gate"],
        health_monitor_report=inputs["health_monitor_report"],
        active_registry=inputs["active_registry"],
        policy_compatibility_summary_path=corrupt_path,
        policy_observability_consumers_path=inventory_path,
    )
    verdict_corrupt = build_launch_verdict(
        cycle_id="c-corrupt",
        preflight_check=preflight,
        launch_checklist={"steps": []},
        readiness_dashboard=readiness_corrupt,
        admission_gate=inputs["admission_gate"],
        policy_compatibility_summary_path=corrupt_path,
        policy_observability_consumers_path=inventory_path,
    )
    human_corrupt = build_human_ops_summary(
        day_id="c-corrupt",
        trading_day_status={"current_stage": "archived"},
        ops_control_center={"live_enabled": False, "system_mode": "paper", "active_batches": [], "active_candidates": []},
        end_of_day_review={"orders_summary": {"total_orders": 0}, "fills_summary": {"total_fills": 0}, "candidate_outcomes": []},
        weekly_candidate_review={"promote": [], "demote": [], "retire": [], "watchlist": []},
        review_plan_patch={"actions": []},
        output_dir=tmp_path,
        policy_compatibility_summary_path=corrupt_path,
        policy_observability_consumers_path=inventory_path,
    )["human_ops_summary"]
    assert readiness_corrupt["policy_compatibility_observability"] == verdict_corrupt["policy_compatibility_observability"] == human_corrupt["policy_compatibility_observability"]
    assert readiness_corrupt["policy_compatibility_observability"]["status"] == "degraded"
    assert readiness_corrupt["policy_compatibility_observability"]["migration_progress"]["status"] == "ok"
    assert readiness_corrupt["policy_compatibility_observability"].get("notice_lifecycle")
