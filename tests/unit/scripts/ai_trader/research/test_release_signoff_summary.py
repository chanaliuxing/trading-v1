from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_signoff_summary import build_release_signoff_summary
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path
    state = root / "runs/ai_trader/research/state"
    cycle = root / "runs/ai_trader/research/cycles/fixture-2026-04-05"
    preflight_release = state / "preflight_release_check.json"
    gate_report = state / "research_safety_contract_gate_report.json"
    signoff_report = state / "release_signoff_summary.json"

    _write_json(preflight_release, {"status": "pass", "overall_status": "ready"})
    _write_json(state / "policy_compatibility_summary.json", {})
    _write_json(state / "policy_observability_consumers.json", {})
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

    view = {
        "policy_compatibility": {"policy_state": "known", "history_count": 1},
        "policy_compatibility_observability": {
            "status": "ok",
            "reason": "ready",
            "source_path": "x",
            "counts": {},
            "current_cycle": {},
            "recent_examples": {},
            "recent_hits": {},
            "migration_progress": {},
            "migration_notice": {},
            "notice_lifecycle": {},
            "notice_lifecycle_state_status": "ok",
        },
    }
    _write_json(cycle / "readiness_dashboard.json", view)
    _write_json(cycle / "launch_verdict.json", view)
    _write_json(cycle / "human_ops_summary.json", view)

    baseline = {
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
    }
    baseline_path = root / "baseline.json"
    _write_json(baseline_path, baseline)
    _write_json(gate_report, {"gate_status": "pass", "execution_guard_status": {"passed": True, "checks": []}})
    return baseline_path, preflight_release, gate_report, signoff_report


def test_all_green_signoff_ready_true(tmp_path: Path) -> None:
    baseline, preflight, gate, signoff = _seed_workspace(tmp_path)
    out = build_release_signoff_summary(
        preflight_release_check_path=preflight,
        freeze_baseline_path=baseline,
        freeze_gate_report_path=gate,
        output_path=signoff,
    )["release_signoff_summary"]
    assert out["signoff_ready"] is True
    assert out["blockers"] == []


def test_preflight_fail_signoff_ready_false(tmp_path: Path) -> None:
    baseline, preflight, gate, signoff = _seed_workspace(tmp_path)
    _write_json(preflight, {"status": "fail", "overall_status": "blocked"})
    out = build_release_signoff_summary(
        preflight_release_check_path=preflight,
        freeze_baseline_path=baseline,
        freeze_gate_report_path=gate,
        output_path=signoff,
    )["release_signoff_summary"]
    assert out["signoff_ready"] is False
    assert "preflight_not_passed" in out["blockers"]


def test_freeze_gate_hold_signoff_ready_false(tmp_path: Path) -> None:
    baseline, preflight, gate, signoff = _seed_workspace(tmp_path)
    _write_json(gate, {"gate_status": "hold", "execution_guard_status": {"passed": True, "checks": []}})
    out = build_release_signoff_summary(
        preflight_release_check_path=preflight,
        freeze_baseline_path=baseline,
        freeze_gate_report_path=gate,
        output_path=signoff,
    )["release_signoff_summary"]
    assert out["signoff_ready"] is False
    assert "freeze_gate_not_passed" in out["blockers"]


def test_missing_required_artifact_signoff_ready_false(tmp_path: Path) -> None:
    baseline, preflight, gate, signoff = _seed_workspace(tmp_path)
    baseline_payload = json.loads(Path(baseline).read_text(encoding="utf-8"))
    Path(baseline_payload["core_state_files"][0]).unlink(missing_ok=True)
    out = build_release_signoff_summary(
        preflight_release_check_path=preflight,
        freeze_baseline_path=baseline,
        freeze_gate_report_path=gate,
        output_path=signoff,
    )["release_signoff_summary"]
    assert out["signoff_ready"] is False
    assert "required_artifacts_missing" in out["blockers"]


def test_execution_guard_fail_signoff_ready_false(tmp_path: Path) -> None:
    baseline, preflight, gate, signoff = _seed_workspace(tmp_path)
    _write_json(gate, {"gate_status": "pass", "execution_guard_status": {"passed": False, "checks": []}})
    out = build_release_signoff_summary(
        preflight_release_check_path=preflight,
        freeze_baseline_path=baseline,
        freeze_gate_report_path=gate,
        output_path=signoff,
    )["release_signoff_summary"]
    assert out["signoff_ready"] is False
    assert "execution_guard_not_passed" in out["blockers"]


def test_real_fixture_signoff_summary_persisted() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    out = build_release_signoff_summary()["release_signoff_summary"]
    assert Path("runs/ai_trader/research/state/release_signoff_summary.json").exists()
    assert set(out.keys()) >= {
        "preflight_status",
        "freeze_gate_status",
        "execution_guard_status",
        "required_artifacts_present",
        "signoff_ready",
        "blockers",
        "recommended_actions",
    }

