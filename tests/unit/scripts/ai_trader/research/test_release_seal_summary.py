from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.candidate_batch_audit_summary import build_candidate_batch_audit_summary
from scripts.ai_trader.research.candidate_batch_record import append_candidate_batch_record
from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_audit_package import build_release_audit_package
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
    gate = state / "research_safety_contract_gate_report.json"
    signoff = state / "release_signoff_summary.json"
    audit = state / "release_candidate_batch_audit_summary.json"
    package = state / "release_audit_package_manifest.json"
    _write_json(gate, {"gate_status": "pass"})
    _write_json(signoff, {"signoff_ready": True, "blockers": []})
    _write_json(audit, {"status": "ok", "total_records": 3})
    _write_json(package, {"package_id": "pkg-x"})
    return {
        "baseline": baseline,
        "gate": gate,
        "signoff": signoff,
        "audit": audit,
        "package": package,
        "seal": state / "release_seal.json",
        "summary": state / "release_final_delivery_summary.json",
    }


def test_seal_manifest_generation() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        out = build_release_seal_and_final_summary(
            release_tag="r35-a",
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            batch_audit_summary_path=paths["audit"],
            audit_package_manifest_path=paths["package"],
            release_seal_path=paths["seal"],
            release_final_summary_path=paths["summary"],
        )
        seal = out["release_seal"]
        assert seal["release_tag"] == "r35-a"
        assert seal["sealed"] is True
        assert Path(out["release_seal_json"]).exists()


def test_final_delivery_summary_generation_and_completeness() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        out = build_release_seal_and_final_summary(
            release_tag="r35-b",
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            batch_audit_summary_path=paths["audit"],
            audit_package_manifest_path=paths["package"],
            release_seal_path=paths["seal"],
            release_final_summary_path=paths["summary"],
        )
        summary = out["release_final_delivery_summary"]
        assert Path(out["release_final_delivery_summary_json"]).exists()
        assert summary["completed_capabilities"]
        assert summary["key_artifact_paths"]
        assert summary["freeze_constraints_summary"]
        assert summary["known_blockers_or_risks"]
        assert summary["change_control_note"]


def test_digest_and_linkage_consistency() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        out = build_release_seal_and_final_summary(
            release_tag="r35-c",
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            batch_audit_summary_path=paths["audit"],
            audit_package_manifest_path=paths["package"],
            release_seal_path=paths["seal"],
            release_final_summary_path=paths["summary"],
        )
        seal = out["release_seal"]
        summary = out["release_final_delivery_summary"]
        assert len(seal["baseline_digest"]) == 64
        assert summary["linkage"]["baseline_digest"] == seal["baseline_digest"]
        assert summary["linkage"]["audit_package_id"] == seal["audit_package_id"]


def test_missing_upstream_fallback() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        paths = _seed_upstream(Path(tdir))
        Path(paths["package"]).unlink(missing_ok=True)
        out = build_release_seal_and_final_summary(
            release_tag="r35-d",
            baseline_path=paths["baseline"],
            gate_report_path=paths["gate"],
            signoff_summary_path=paths["signoff"],
            batch_audit_summary_path=paths["audit"],
            audit_package_manifest_path=paths["package"],
            release_seal_path=paths["seal"],
            release_final_summary_path=paths["summary"],
        )
        assert out["release_seal"]["sealed"] is False
        assert out["release_final_delivery_summary"]["status"] == "degraded"


def test_real_package_linkage_and_persisted_outputs() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    build_release_signoff_summary()
    append_candidate_batch_record(
        batch_id="phase35-seal",
        run_ids=["phase35-run"],
        cycle_ids=["fixture-2026-04-05"],
    )
    build_candidate_batch_audit_summary(keep_last_records=10)
    build_release_audit_package(package_id="phase35-seal-package")
    out = build_release_seal_and_final_summary(release_tag="phase35-seal")
    seal_path = Path(out["release_seal_json"])
    summary_path = Path(out["release_final_delivery_summary_json"])
    assert seal_path.exists()
    assert summary_path.exists()
    assert out["release_seal"]["audit_package_id"] == "phase35-seal-package"

