from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.release_delivery_index import build_release_delivery_index
from scripts.ai_trader.research.release_handoff import build_release_handoff_artifacts
from scripts.ai_trader.research.release_maintenance_verification import run_maintenance_verification
from scripts.ai_trader.research.release_seal_summary import build_release_seal_and_final_summary


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_state() -> None:
    state = Path("runs/ai_trader/research/state")
    baseline = Path("docs/research_safety_contract_freeze_baseline.json")
    if not baseline.exists():
        _write_json(baseline, {"freeze_constraints": {"summary": "x"}})
    _write_json(state / "research_safety_contract_gate_report.json", {"gate_status": "pass", "execution_guard_status": {"passed": True, "checks": []}})
    _write_json(state / "release_signoff_summary.json", {"signoff_ready": False, "signoff_status": "not_ready", "blockers": ["preflight_not_passed"]})
    _write_json(state / "release_audit_package_manifest.json", {"package_id": "m1-pkg"})
    _write_json(state / "release_candidate_batch_audit_summary.json", {"status": "ok", "total_records": 1})
    _write_json(state / "release_handoff_manifest.json", {"handoff_status": "ready", "sealed_release_tag": "m1-seal", "linked_package_id": "m1-pkg"})
    _write_json(
        state / "release_prechange_checklist_template.json",
        {
            "must_check_freeze_baseline": True,
            "must_run_freeze_gate": True,
            "must_review_signoff_summary": True,
            "must_preserve_execution_guard": True,
            "must_not_change_overall_status_semantics": True,
            "must_update_baseline_if_contract_changes": True,
        },
    )
    build_release_seal_and_final_summary(release_tag="m1-seal")
    build_release_handoff_artifacts(handoff_id="m1-handoff")
    build_release_delivery_index(index_id="m1-index")


def test_maintenance_verification_record_generation() -> None:
    _seed_state()
    out = run_maintenance_verification(verification_id="m1-verify-a")
    entry = out["entry"]
    assert entry["verification_id"] == "m1-verify-a"
    assert Path(out["release_maintenance_verifications_json"]).exists()


def test_no_change_gate_signoff_index_consistency() -> None:
    _seed_state()
    out = run_maintenance_verification(verification_id="m1-verify-b")
    entry = out["entry"]
    assert entry["freeze_gate_status"] == "pass"
    assert entry["signoff_status"] in {"ready", "not_ready"}
    assert entry["delivery_index_status"] in {"ok", "degraded"}


def test_artifact_paths_still_valid() -> None:
    _seed_state()
    out = run_maintenance_verification(verification_id="m1-verify-c")
    entry = out["entry"]
    assert entry["path_integrity"]["missing_paths"] == []
    assert entry["path_integrity"]["required_paths"]


def test_missing_upstream_fallback() -> None:
    _seed_state()
    template = Path("runs/ai_trader/research/state/release_prechange_checklist_template.json")
    template.unlink(missing_ok=True)
    out = run_maintenance_verification(verification_id="m1-verify-d")
    entry = out["entry"]
    assert entry["path_integrity"]["missing_paths"]
    assert any("missing_required_paths_detected" == note for note in entry["notes"])

