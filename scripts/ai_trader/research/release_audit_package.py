from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"
DEFAULT_BASELINE_PATH = "docs/research_safety_contract_freeze_baseline.json"
DEFAULT_PACKAGE_MANIFEST_PATH = "runs/ai_trader/research/state/release_audit_package_manifest.json"


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: str | Path) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _default_artifact_paths(*, state_root: str | Path, baseline_path: str | Path) -> list[str]:
    root = Path(state_root)
    return [
        str(Path(baseline_path)),
        str(root / "research_safety_contract_gate_report.json"),
        str(root / "release_signoff_summary.json"),
        str(root / "release_candidate_batch_records.json"),
        str(root / "release_candidate_batch_audit_summary.json"),
        str(root / "release_candidate_batch_query_export.json"),
    ]


def build_release_audit_package(
    *,
    package_id: str | None = None,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    package_manifest_path: str | Path = DEFAULT_PACKAGE_MANIFEST_PATH,
    artifact_paths: list[str] | None = None,
) -> Dict[str, Any]:
    state_dir = Path(state_root)
    package_key = str(package_id or f"release-audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    package_dir = state_dir / "release_audit_packages" / package_key
    package_dir.mkdir(parents=True, exist_ok=True)
    package_artifact_dir = package_dir / "artifacts"
    package_artifact_dir.mkdir(parents=True, exist_ok=True)

    sources = artifact_paths or _default_artifact_paths(state_root=state_root, baseline_path=baseline_path)
    included_artifacts: list[Dict[str, Any]] = []
    artifact_digests: Dict[str, str] = {}
    missing_artifacts: list[str] = []

    for source_text in sources:
        source = Path(source_text)
        source_key = str(source)
        exists = source.exists()
        packaged_path = package_artifact_dir / source.name
        digest = ""
        if exists:
            digest = _sha256(source)
            shutil.copy2(source, packaged_path)
        else:
            missing_artifacts.append(source_key)
        artifact_digests[source_key] = digest
        included_artifacts.append(
            {
                "source_path": source_key,
                "packaged_path": str(packaged_path),
                "exists": exists,
            }
        )

    signoff = _load_json(Path(state_root) / "release_signoff_summary.json")
    gate = _load_json(Path(state_root) / "research_safety_contract_gate_report.json")
    baseline_file = Path(baseline_path)
    baseline_digest = _sha256(baseline_file) if baseline_file.exists() else ""
    signoff_status = "ready" if bool(_as_dict(signoff).get("signoff_ready", False)) else "not_ready"
    gate_status = str(_as_dict(gate).get("gate_status") or "hold")

    notes = [
        "snapshot_only_no_rule_changes",
        "digest_reproducible_sha256",
    ]
    if missing_artifacts:
        notes.append("missing_artifacts_detected")

    manifest = {
        "package_id": package_key,
        "created_at": _iso_now(),
        "included_artifacts": included_artifacts,
        "artifact_digests": artifact_digests,
        "baseline_digest": baseline_digest,
        "signoff_status": signoff_status,
        "gate_status": gate_status,
        "notes": notes,
        "missing_artifacts": missing_artifacts,
        "package_status": "degraded" if missing_artifacts else "ok",
        "package_dir": str(package_dir),
    }

    package_manifest = package_dir / "manifest.json"
    package_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    top_manifest = Path(package_manifest_path)
    top_manifest.parent.mkdir(parents=True, exist_ok=True)
    top_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "release_audit_package_manifest_json": str(top_manifest),
        "release_audit_package_manifest": manifest,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build release audit package snapshot")
    parser.add_argument("--package-id", default="")
    args = parser.parse_args()
    bundle = build_release_audit_package(package_id=str(args.package_id or "") or None)
    payload = _as_dict(bundle.get("release_audit_package_manifest"))
    print("release_audit_package_manifest:", bundle.get("release_audit_package_manifest_json"))
    print("package_id:", payload.get("package_id"))
    print("package_status:", payload.get("package_status"))


if __name__ == "__main__":
    main()

