from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.candidate_batch_audit_summary import build_candidate_batch_audit_summary
from scripts.ai_trader.research.candidate_batch_record import append_candidate_batch_record
from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_audit_package import build_release_audit_package
from scripts.ai_trader.research.release_handoff import build_release_handoff_artifacts
from scripts.ai_trader.research.release_seal_summary import build_release_seal_and_final_summary
from scripts.ai_trader.research.release_signoff_summary import build_release_signoff_summary
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_upstream(tmp_path: Path) -> dict:
    baseline = tmp_path / "docs" / "research_safety_contract_freeze_baseline.json"
    _write_json(baseline, {"freeze_constraints": {"summary": "x", "must_not_change": ["overall_status_semantics"]}})
    state = tmp_path / "runs/ai_trader/research/state"
    seal = state / "release_seal.json"
    final_summary = state / "release_final_delivery_summary.json"
    package = state / "release_audit_package_manifest.json"
    gate = state / "research_safety_contract_gate_report.json"
    signoff = state / "release_signoff_summary.json"
    _write_json(seal, {"release_tag": "tag-x", "sealed": True})
    _write_json(final_summary, {"known_blockers_or_risks": ["none"], "status": "ok"})
    _write_json(package, {"package_id": "pkg-x"})
    _write_json(gate, {"gate_status": "pass"})
    _write_json(signoff, {"signoff_ready": False, "blockers": ["preflight_not_passed"]})
    return {
        "baseline": baseline,
        "seal": seal,
        "final_summary": final_summary,
        "package": package,
        "gate": gate,
        "signoff": signoff,
        "handoff": state / "release_handoff_manifest.json",
        "template": state / "release_prechange_checklist_template.json",
    }


def test_handoff_manifest_generation() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        out = build_release_handoff_artifacts(
            handoff_id="handoff-a",
            seal_path=paths["seal"],
            final_summary_path=paths["final_summary"],
            audit_package_manifest_path=paths["package"],
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            handoff_manifest_path=paths["handoff"],
            prechange_template_path=paths["template"],
        )
        manifest = out["release_handoff_manifest"]
        assert manifest["handoff_id"] == "handoff-a"
        assert Path(out["release_handoff_manifest_json"]).exists()


def test_prechange_checklist_template_completeness() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        out = build_release_handoff_artifacts(
            handoff_id="handoff-b",
            seal_path=paths["seal"],
            final_summary_path=paths["final_summary"],
            audit_package_manifest_path=paths["package"],
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            handoff_manifest_path=paths["handoff"],
            prechange_template_path=paths["template"],
        )
        template = out["release_prechange_checklist_template"]
        for field in (
            "must_check_freeze_baseline",
            "must_run_freeze_gate",
            "must_review_signoff_summary",
            "must_preserve_execution_guard",
            "must_not_change_overall_status_semantics",
            "must_update_baseline_if_contract_changes",
        ):
            assert template[field] is True


def test_linkage_consistency_to_seal_package_baseline() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        out = build_release_handoff_artifacts(
            handoff_id="handoff-c",
            seal_path=paths["seal"],
            final_summary_path=paths["final_summary"],
            audit_package_manifest_path=paths["package"],
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            handoff_manifest_path=paths["handoff"],
            prechange_template_path=paths["template"],
        )
        manifest = out["release_handoff_manifest"]
        assert manifest["sealed_release_tag"] == "tag-x"
        assert manifest["linked_package_id"] == "pkg-x"
        assert len(manifest["baseline_digest"]) == 64


def test_missing_upstream_fallback_degraded() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        Path(paths["seal"]).unlink(missing_ok=True)
        out = build_release_handoff_artifacts(
            handoff_id="handoff-d",
            seal_path=paths["seal"],
            final_summary_path=paths["final_summary"],
            audit_package_manifest_path=paths["package"],
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            handoff_manifest_path=paths["handoff"],
            prechange_template_path=paths["template"],
        )
        manifest = out["release_handoff_manifest"]
        assert manifest["handoff_status"] == "degraded"
        assert manifest["missing_required_artifacts"]


def test_real_sealed_release_to_handoff_persisted() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    build_release_signoff_summary()
    append_candidate_batch_record(
        batch_id="phase36-handoff",
        run_ids=["phase36-run"],
        cycle_ids=["fixture-2026-04-05"],
    )
    build_candidate_batch_audit_summary(keep_last_records=10)
    build_release_audit_package(package_id="phase36-handoff-package")
    build_release_seal_and_final_summary(release_tag="phase36-handoff")

    out = build_release_handoff_artifacts(handoff_id="phase36-handoff")
    assert Path(out["release_handoff_manifest_json"]).exists()
    assert Path(out["release_prechange_checklist_template_json"]).exists()
    assert out["release_handoff_manifest"]["linked_package_id"] == "phase36-handoff-package"

