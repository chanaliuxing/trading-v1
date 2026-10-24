from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"
DEFAULT_BASELINE_PATH = "docs/research_safety_contract_freeze_baseline.json"
DEFAULT_GATE_REPORT_PATH = "runs/ai_trader/research/state/research_safety_contract_gate_report.json"
DEFAULT_SIGNOFF_SUMMARY_PATH = "runs/ai_trader/research/state/release_signoff_summary.json"
DEFAULT_BATCH_AUDIT_SUMMARY_PATH = "runs/ai_trader/research/state/release_candidate_batch_audit_summary.json"
DEFAULT_AUDIT_PACKAGE_MANIFEST_PATH = "runs/ai_trader/research/state/release_audit_package_manifest.json"
DEFAULT_RELEASE_SEAL_PATH = "runs/ai_trader/research/state/release_seal.json"
DEFAULT_RELEASE_FINAL_SUMMARY_PATH = "runs/ai_trader/research/state/release_final_delivery_summary.json"


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


def _load_json_status(path: str | Path) -> tuple[str, str, Dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return "unavailable", "missing_file", {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return "degraded", "corrupt_file", {}
    if not isinstance(payload, dict):
        return "degraded", "invalid_payload", {}
    return "ok", "ready", payload


def build_release_seal_and_final_summary(
    *,
    release_tag: str | None = None,
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    gate_report_path: str | Path = DEFAULT_GATE_REPORT_PATH,
    signoff_summary_path: str | Path = DEFAULT_SIGNOFF_SUMMARY_PATH,
    batch_audit_summary_path: str | Path = DEFAULT_BATCH_AUDIT_SUMMARY_PATH,
    audit_package_manifest_path: str | Path = DEFAULT_AUDIT_PACKAGE_MANIFEST_PATH,
    release_seal_path: str | Path = DEFAULT_RELEASE_SEAL_PATH,
    release_final_summary_path: str | Path = DEFAULT_RELEASE_FINAL_SUMMARY_PATH,
) -> Dict[str, Any]:
    tag = str(release_tag or f"release-seal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    baseline_status, baseline_reason, baseline = _load_json_status(baseline_path)
    gate_status_raw, gate_reason, gate = _load_json_status(gate_report_path)
    signoff_status_raw, signoff_reason, signoff = _load_json_status(signoff_summary_path)
    audit_status_raw, audit_reason, audit_summary = _load_json_status(batch_audit_summary_path)
    package_status_raw, package_reason, package_manifest = _load_json_status(audit_package_manifest_path)

    upstream_states = {
        str(baseline_path): (baseline_status, baseline_reason),
        str(gate_report_path): (gate_status_raw, gate_reason),
        str(signoff_summary_path): (signoff_status_raw, signoff_reason),
        str(batch_audit_summary_path): (audit_status_raw, audit_reason),
        str(audit_package_manifest_path): (package_status_raw, package_reason),
    }
    missing_upstream = [path for path, (status, _) in upstream_states.items() if status != "ok"]

    baseline_file = Path(baseline_path)
    baseline_digest = _sha256(baseline_file) if baseline_file.exists() else ""
    gate_status = str(_as_dict(gate).get("gate_status") or "hold")
    signoff_status = "ready" if bool(_as_dict(signoff).get("signoff_ready", False)) else "not_ready"
    audit_package_id = str(_as_dict(package_manifest).get("package_id") or "")

    sealed = len(missing_upstream) == 0
    notes = [
        "sealed_release_snapshot_for_change_reference",
        "subsequent_changes_must_pass_freeze_change_gate",
    ]
    if missing_upstream:
        notes.append("upstream_artifact_missing_or_degraded")

    seal_payload = {
        "release_tag": tag,
        "created_at": _iso_now(),
        "baseline_digest": baseline_digest,
        "gate_status": gate_status,
        "signoff_status": signoff_status,
        "audit_package_id": audit_package_id,
        "sealed": sealed,
        "notes": notes,
        "upstream_status": {
            path: {"status": status, "reason": reason}
            for path, (status, reason) in upstream_states.items()
        },
    }

    blockers = [str(x) for x in _as_list(_as_dict(signoff).get("blockers")) if str(x)]
    if not blockers and missing_upstream:
        blockers = [f"missing_or_degraded:{path}" for path in missing_upstream]

    freeze_constraints = _as_dict(_as_dict(baseline).get("freeze_constraints"))
    final_summary = {
        "release_tag": tag,
        "created_at": _iso_now(),
        "completed_capabilities": [
            "freeze_baseline",
            "freeze_change_gate",
            "release_signoff_summary",
            "candidate_batch_record",
            "candidate_batch_audit_summary",
            "candidate_batch_query_export",
            "release_audit_package",
            "release_seal",
        ],
        "key_artifact_paths": {
            "freeze_baseline": str(baseline_path),
            "gate_report": str(gate_report_path),
            "signoff_summary": str(signoff_summary_path),
            "batch_audit_summary": str(batch_audit_summary_path),
            "audit_package_manifest": str(audit_package_manifest_path),
            "release_seal": str(release_seal_path),
            "release_final_delivery_summary": str(release_final_summary_path),
        },
        "freeze_constraints_summary": {
            "summary": str(freeze_constraints.get("summary") or "freeze_gate_required_before_any_change"),
            "must_not_change": [str(x) for x in _as_list(freeze_constraints.get("must_not_change")) if str(x)],
        },
        "known_blockers_or_risks": blockers if blockers else ["none"],
        "change_control_note": "Any subsequent change must pass freeze_change_gate before signoff.",
        "linkage": {
            "baseline_digest": baseline_digest,
            "audit_package_id": audit_package_id,
            "gate_status": gate_status,
            "signoff_status": signoff_status,
            "sealed": sealed,
        },
        "status": "ok" if sealed else "degraded",
    }

    seal_file = Path(release_seal_path)
    summary_file = Path(release_final_summary_path)
    seal_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    seal_file.write_text(json.dumps(seal_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_file.write_text(json.dumps(final_summary, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "release_seal_json": str(seal_file),
        "release_final_delivery_summary_json": str(summary_file),
        "release_seal": seal_payload,
        "release_final_delivery_summary": final_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate release seal and final delivery summary")
    parser.add_argument("--release-tag", default="")
    args = parser.parse_args()
    bundle = build_release_seal_and_final_summary(release_tag=str(args.release_tag or "") or None)
    seal = _as_dict(bundle.get("release_seal"))
    print("release_seal:", bundle.get("release_seal_json"))
    print("release_final_delivery_summary:", bundle.get("release_final_delivery_summary_json"))
    print("sealed:", seal.get("sealed"))


if __name__ == "__main__":
    main()

