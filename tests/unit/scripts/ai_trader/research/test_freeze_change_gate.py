from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_minimal_view() -> dict:
    return {
        "policy_compatibility": {"policy_state": "known", "history_count": 1},
        "policy_compatibility_observability": {
            "status": "ok",
            "reason": "ready",
            "source_path": "state/policy_compatibility_summary.json",
            "counts": {"known": 1, "legacy": 0, "unknown": 0, "degraded": 0, "digest_changed_without_version_bump": 0},
            "current_cycle": {"cycle_id": "c-1"},
            "recent_examples": {"legacy": [], "unknown": [], "degraded": [], "digest_changed_without_version_bump": []},
            "recent_hits": {"legacy": [], "unknown": [], "degraded": [], "digest_changed_without_version_bump": []},
            "migration_progress": {
                "status": "ok",
                "reason": "ready",
                "source_path": "state/policy_observability_consumers.json",
                "runtime_legacy_consumer_count": 0,
                "total_runtime_consumers": 0,
                "migrated_runtime_consumers": 0,
                "docs_tests_only_references": 0,
                "progress_ratio": 1.0,
            },
            "migration_notice": {
                "status": "none",
                "level": "none",
                "legacy_runtime_consumer_count": 0,
                "summary": "none",
                "details": {},
                "throttled": True,
                "throttle_key": "none:0",
                "legacy_fields": ["policy_compatibility", "recent_hits"],
                "recommended_field": "policy_compatibility_observability",
                "top_legacy_consumers": [],
                "active": False,
                "silenced": False,
                "lifecycle_status": "inactive",
                "highlight": False,
                "lifecycle_state_path": "state/policy_observability_notice_lifecycle.json",
            },
            "notice_lifecycle": {
                "active": False,
                "silenced": False,
                "lifecycle_status": "inactive",
                "first_seen": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-01-01T00:00:00+00:00",
                "silenced_until": "",
                "last_level": "none",
                "last_throttle_key": "none:0",
            },
            "notice_lifecycle_state_status": "ok",
        },
    }


def _seed_gate_workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path
    state = root / "runs/ai_trader/research/state"
    cycle = root / "runs/ai_trader/research/cycles/fixture-2026-04-05"
    _write_json(state / "policy_compatibility_summary.json", {"summary": {}})
    _write_json(state / "policy_observability_consumers.json", {"summary": {}, "entries": []})
    _write_json(
        state / "policy_observability_notice_lifecycle.json",
        {
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "last_level": "none",
            "last_throttle_key": "none:0",
            "silenced_until": "",
        },
    )
    _write_json(state / "launch_registry.json", {})
    _write_json(state / "system_status.json", {})

    view = _build_minimal_view()
    _write_json(cycle / "readiness_dashboard.json", view)
    _write_json(cycle / "launch_verdict.json", view)
    _write_json(cycle / "human_ops_summary.json", view)

    guard_a = root / "guard_a.py"
    guard_b = root / "guard_b.py"
    guard_a.write_text('overall_status = str(preflight.get("overall_status") or "blocked")\n', encoding="utf-8")
    guard_b.write_text('mode="small_live" if str(launch_verdict.get("verdict")) == "go_small_live" else "paper"\n', encoding="utf-8")

    baseline = {
        "manifest_version": "test",
        "scope": "ai_trader_research_safety_observability_readonly_contract",
        "core_state_files": [
            str(state / "policy_compatibility_summary.json"),
            str(state / "policy_observability_consumers.json"),
            str(state / "policy_observability_notice_lifecycle.json"),
            str(state / "launch_registry.json"),
            str(state / "system_status.json"),
        ],
        "readonly_view_files": [
            str(cycle / "readiness_dashboard.json"),
            str(cycle / "launch_verdict.json"),
            str(cycle / "human_ops_summary.json"),
        ],
        "contract_fields": {
            "policy_compatibility_observability": [
                "status",
                "reason",
                "source_path",
                "counts",
                "current_cycle",
                "recent_examples",
                "recent_hits",
                "migration_progress",
                "migration_notice",
                "notice_lifecycle",
                "notice_lifecycle_state_status",
            ],
            "migration_notice": [
                "status",
                "level",
                "legacy_runtime_consumer_count",
                "summary",
                "details",
                "throttled",
                "throttle_key",
                "legacy_fields",
                "recommended_field",
                "top_legacy_consumers",
                "active",
                "silenced",
                "lifecycle_status",
                "highlight",
                "lifecycle_state_path",
            ],
            "notice_lifecycle": [
                "active",
                "silenced",
                "lifecycle_status",
                "first_seen",
                "last_seen",
                "silenced_until",
                "last_level",
                "last_throttle_key",
            ],
            "notice_lifecycle_state_file": [
                "first_seen",
                "last_seen",
                "last_level",
                "last_throttle_key",
                "silenced_until",
            ],
            "migration_progress": [
                "status",
                "reason",
                "source_path",
                "runtime_legacy_consumer_count",
                "total_runtime_consumers",
                "migrated_runtime_consumers",
                "docs_tests_only_references",
                "progress_ratio",
            ],
            "three_entry_consistency": [
                "status",
                "level",
                "active",
                "silenced",
                "throttle_key",
                "first_seen",
                "last_seen",
                "last_level",
                "last_throttle_key",
                "silenced_until",
            ],
        },
        "compatibility_aliases": [
            {"alias": "recent_hits", "canonical": "recent_examples", "relation": "exact_equal"},
            {
                "alias": "policy_compatibility",
                "canonical": "policy_compatibility_observability.current_cycle",
                "relation": "legacy_projection",
            },
        ],
        "execution_chain_unchanged_guards": [
            {
                "file": str(guard_a),
                "must_contain": 'overall_status = str(preflight.get("overall_status") or "blocked")',
            },
            {
                "file": str(guard_b),
                "must_contain": 'mode="small_live" if str(launch_verdict.get("verdict")) == "go_small_live" else "paper"',
            },
        ],
    }
    baseline_path = root / "baseline.json"
    _write_json(baseline_path, baseline)
    report_path = root / "report.json"
    return baseline_path, report_path


def test_no_frozen_scope_changes_pass(tmp_path: Path) -> None:
    baseline_path, report_path = _seed_gate_workspace(tmp_path)
    report = evaluate_freeze_change_gate(baseline_path=baseline_path, report_path=report_path, persist=True)
    assert report["gate_status"] == "pass"
    assert report["changed_artifacts"] == []
    assert report["changed_contract_fields"] == []
    assert report["changed_aliases"] == []
    assert report["execution_guard_status"]["passed"] is True
    assert report_path.exists()


def test_contract_field_drift_hold(tmp_path: Path) -> None:
    baseline_path, report_path = _seed_gate_workspace(tmp_path)
    readiness_path = Path(json.loads(baseline_path.read_text(encoding="utf-8"))["readonly_view_files"][0])
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    readiness["policy_compatibility_observability"]["migration_progress"].pop("progress_ratio", None)
    readiness_path.write_text(json.dumps(readiness, indent=2, ensure_ascii=False), encoding="utf-8")

    report = evaluate_freeze_change_gate(baseline_path=baseline_path, report_path=report_path, persist=False)
    assert report["gate_status"] == "hold"
    assert report["changed_contract_fields"]
    assert "frozen_contract_field_drift_detected" in report["reasons"]


def test_alias_drift_hold(tmp_path: Path) -> None:
    baseline_path, report_path = _seed_gate_workspace(tmp_path)
    readiness_path = Path(json.loads(baseline_path.read_text(encoding="utf-8"))["readonly_view_files"][0])
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    readiness["policy_compatibility_observability"]["recent_hits"] = {"legacy": ["x"]}
    readiness_path.write_text(json.dumps(readiness, indent=2, ensure_ascii=False), encoding="utf-8")

    report = evaluate_freeze_change_gate(baseline_path=baseline_path, report_path=report_path, persist=False)
    assert report["gate_status"] == "hold"
    assert report["changed_aliases"]
    assert "compatibility_alias_drift_detected" in report["reasons"]


def test_execution_guard_missing_hold(tmp_path: Path) -> None:
    baseline_path, report_path = _seed_gate_workspace(tmp_path)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    guard_file = Path(baseline["execution_chain_unchanged_guards"][0]["file"])
    guard_file.write_text("placeholder = True\n", encoding="utf-8")

    report = evaluate_freeze_change_gate(baseline_path=baseline_path, report_path=report_path, persist=False)
    assert report["gate_status"] == "hold"
    assert report["execution_guard_status"]["passed"] is False
    assert "execution_chain_guard_missing" in report["reasons"]


def test_baseline_refreshed_then_pass(tmp_path: Path) -> None:
    baseline_path, report_path = _seed_gate_workspace(tmp_path)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    for view_path in baseline["readonly_view_files"]:
        target = Path(view_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["policy_compatibility_observability"]["migration_notice"]["new_notice_field"] = "x"
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    first = evaluate_freeze_change_gate(baseline_path=baseline_path, report_path=report_path, persist=False)
    assert first["gate_status"] == "hold"

    baseline["contract_fields"]["migration_notice"].append("new_notice_field")
    baseline_path.write_text(json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    second = evaluate_freeze_change_gate(baseline_path=baseline_path, report_path=report_path, persist=False)
    assert second["gate_status"] == "pass"


def test_real_fixture_gate_report_persisted() -> None:
    run_fixture_cycle_main()
    report = evaluate_freeze_change_gate()
    assert report["report_path"] == "runs/ai_trader/research/state/research_safety_contract_gate_report.json"
    assert Path(report["report_path"]).exists()
