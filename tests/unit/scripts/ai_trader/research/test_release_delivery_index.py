from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.ai_trader.research.release_delivery_index import build_release_delivery_index


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_upstream(tmp_path: Path) -> dict:
    baseline = tmp_path / "docs" / "research_safety_contract_freeze_baseline.json"
    _write_json(baseline, {"freeze_constraints": {"summary": "x"}})
    state = tmp_path / "runs/ai_trader/research/state"
    seal = state / "release_seal.json"
    final_summary = state / "release_final_delivery_summary.json"
    package = state / "release_audit_package_manifest.json"
    handoff = state / "release_handoff_manifest.json"
    template = state / "release_prechange_checklist_template.json"
    signoff = state / "release_signoff_summary.json"

    _write_json(seal, {"release_tag": "phase-x", "gate_status": "pass", "signoff_status": "not_ready"})
    _write_json(final_summary, {"known_blockers_or_risks": ["preflight_not_passed"]})
    _write_json(package, {"package_id": "pkg-x"})
    _write_json(handoff, {"handoff_status": "ready"})
    _write_json(
        template,
        {
            "must_check_freeze_baseline": True,
            "must_run_freeze_gate": True,
            "must_review_signoff_summary": True,
            "must_preserve_execution_guard": True,
            "must_not_change_overall_status_semantics": True,
            "must_update_baseline_if_contract_changes": True,
        },
    )
    _write_json(signoff, {"signoff_ready": False, "blockers": ["preflight_not_passed"]})

    return {
        "baseline": baseline,
        "seal": seal,
        "final_summary": final_summary,
        "package": package,
        "handoff": handoff,
        "template": template,
        "signoff": signoff,
        "index": state / "release_delivery_index.json",
    }


def test_delivery_index_generation_and_completeness(tmp_path: Path) -> None:
    paths = _seed_upstream(tmp_path)
    out = build_release_delivery_index(
        index_id="idx-a",
        baseline_path=paths["baseline"],
        seal_path=paths["seal"],
        final_summary_path=paths["final_summary"],
        package_manifest_path=paths["package"],
        handoff_manifest_path=paths["handoff"],
        prechange_template_path=paths["template"],
        signoff_summary_path=paths["signoff"],
        output_path=paths["index"],
    )["release_delivery_index"]
    assert out["index_id"] == "idx-a"
    assert out["sealed_release_tag"] == "phase-x"
    assert out["key_artifacts"]
    assert len(out["required_commands"]) == 6
    assert Path(paths["index"]).exists()


def test_artifact_linkage_consistency(tmp_path: Path) -> None:
    paths = _seed_upstream(tmp_path)
    out = build_release_delivery_index(
        index_id="idx-b",
        baseline_path=paths["baseline"],
        seal_path=paths["seal"],
        final_summary_path=paths["final_summary"],
        package_manifest_path=paths["package"],
        handoff_manifest_path=paths["handoff"],
        prechange_template_path=paths["template"],
        signoff_summary_path=paths["signoff"],
        output_path=paths["index"],
    )["release_delivery_index"]
    assert out["baseline_digest"] == _digest(paths["baseline"])
    assert out["handoff_paths"]["release_handoff_manifest"] == str(paths["handoff"])
    assert out["final_status_summary"]["handoff_status"] == "ready"


def test_readme_pointer_consistency() -> None:
    text = Path("docs/README.md").read_text(encoding="utf-8")
    assert "release_delivery_index" in text
    assert "runs/ai_trader/research/state/release_seal.json" in text
    assert "runs/ai_trader/research/state/release_handoff_manifest.json" in text
    assert "runs/ai_trader/research/state/release_prechange_checklist_template.json" in text


def test_missing_upstream_fallback(tmp_path: Path) -> None:
    paths = _seed_upstream(tmp_path)
    Path(paths["seal"]).unlink(missing_ok=True)
    out = build_release_delivery_index(
        index_id="idx-c",
        baseline_path=paths["baseline"],
        seal_path=paths["seal"],
        final_summary_path=paths["final_summary"],
        package_manifest_path=paths["package"],
        handoff_manifest_path=paths["handoff"],
        prechange_template_path=paths["template"],
        signoff_summary_path=paths["signoff"],
        output_path=paths["index"],
    )["release_delivery_index"]
    assert out["status"] == "degraded"
    assert out["upstream_status"][str(paths["seal"])] ["status"] == "unavailable"

