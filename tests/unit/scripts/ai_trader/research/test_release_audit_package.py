from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.ai_trader.research.candidate_batch_audit_summary import build_candidate_batch_audit_summary
from scripts.ai_trader.research.candidate_batch_record import append_candidate_batch_record
from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_audit_package import build_release_audit_package
from scripts.ai_trader.research.release_signoff_summary import build_release_signoff_summary
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _seed_artifacts(tmp_path: Path) -> tuple[list[str], Path, Path]:
    baseline = tmp_path / "docs" / "research_safety_contract_freeze_baseline.json"
    _write_json(baseline, {"baseline": True})

    state = tmp_path / "runs/ai_trader/research/state"
    paths = [
        state / "research_safety_contract_gate_report.json",
        state / "release_signoff_summary.json",
        state / "release_candidate_batch_records.json",
        state / "release_candidate_batch_audit_summary.json",
        state / "release_candidate_batch_query_export.json",
    ]
    for idx, path in enumerate(paths):
        _write_json(path, {"idx": idx, "name": path.name})

    artifact_paths = [str(baseline)] + [str(x) for x in paths]
    manifest_path = state / "release_audit_package_manifest.json"
    return artifact_paths, baseline, manifest_path


def test_package_manifest_generation_and_layout(tmp_path: Path) -> None:
    artifacts, baseline, manifest_path = _seed_artifacts(tmp_path)
    out = build_release_audit_package(
        package_id="pkg-001",
        state_root=tmp_path / "runs/ai_trader/research/state",
        baseline_path=baseline,
        package_manifest_path=manifest_path,
        artifact_paths=artifacts,
    )["release_audit_package_manifest"]
    assert out["package_id"] == "pkg-001"
    assert Path(manifest_path).exists()
    assert Path(out["package_dir"]).exists()
    assert Path(out["package_dir"]).name == "pkg-001"
    assert len(out["included_artifacts"]) == len(artifacts)


def test_artifact_digest_integrity(tmp_path: Path) -> None:
    artifacts, baseline, manifest_path = _seed_artifacts(tmp_path)
    out = build_release_audit_package(
        package_id="pkg-002",
        state_root=tmp_path / "runs/ai_trader/research/state",
        baseline_path=baseline,
        package_manifest_path=manifest_path,
        artifact_paths=artifacts,
    )["release_audit_package_manifest"]
    for source in artifacts:
        source_path = Path(source)
        assert out["artifact_digests"][str(source_path)] == _digest(source_path)
    assert out["baseline_digest"] == _digest(baseline)


def test_missing_artifact_fallback_degraded(tmp_path: Path) -> None:
    artifacts, baseline, manifest_path = _seed_artifacts(tmp_path)
    missing = artifacts[-1]
    Path(missing).unlink(missing_ok=True)
    out = build_release_audit_package(
        package_id="pkg-003",
        state_root=tmp_path / "runs/ai_trader/research/state",
        baseline_path=baseline,
        package_manifest_path=manifest_path,
        artifact_paths=artifacts,
    )["release_audit_package_manifest"]
    assert out["package_status"] == "degraded"
    assert missing in out["missing_artifacts"]
    assert "missing_artifacts_detected" in out["notes"]


def test_stable_package_layout(tmp_path: Path) -> None:
    artifacts, baseline, manifest_path = _seed_artifacts(tmp_path)
    out = build_release_audit_package(
        package_id="pkg-004",
        state_root=tmp_path / "runs/ai_trader/research/state",
        baseline_path=baseline,
        package_manifest_path=manifest_path,
        artifact_paths=artifacts,
    )["release_audit_package_manifest"]
    for row in out["included_artifacts"]:
        packaged = Path(row["packaged_path"])
        assert packaged.exists() == bool(row["exists"])
        assert str(packaged).startswith(str(Path(out["package_dir"])))


def test_real_chain_artifacts_can_be_packaged() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    build_release_signoff_summary()
    append_candidate_batch_record(
        batch_id="phase34-package",
        run_ids=["phase34-run"],
        cycle_ids=["fixture-2026-04-05"],
    )
    build_candidate_batch_audit_summary(keep_last_records=10)
    query_export = Path("runs/ai_trader/research/state/release_candidate_batch_query_export.json")
    if not query_export.exists():
        _write_json(query_export, {"status": "ok", "matched_records": []})

    out = build_release_audit_package(package_id="phase34-package")
    manifest_path = Path(out["release_audit_package_manifest_json"])
    assert manifest_path.exists()
    payload = out["release_audit_package_manifest"]
    assert payload["package_id"] == "phase34-package"
    assert payload["included_artifacts"]

