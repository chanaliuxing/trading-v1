from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"
DEFAULT_SEAL_PATH = "runs/ai_trader/research/state/release_seal.json"
DEFAULT_FINAL_SUMMARY_PATH = "runs/ai_trader/research/state/release_final_delivery_summary.json"
DEFAULT_AUDIT_PACKAGE_MANIFEST_PATH = "runs/ai_trader/research/state/release_audit_package_manifest.json"
DEFAULT_BASELINE_PATH = "docs/research_safety_contract_freeze_baseline.json"
DEFAULT_GATE_REPORT_PATH = "runs/ai_trader/research/state/research_safety_contract_gate_report.json"
DEFAULT_SIGNOFF_SUMMARY_PATH = "runs/ai_trader/research/state/release_signoff_summary.json"
DEFAULT_HANDOFF_MANIFEST_PATH = "runs/ai_trader/research/state/release_handoff_manifest.json"
DEFAULT_PRECHANGE_TEMPLATE_PATH = "runs/ai_trader/research/state/release_prechange_checklist_template.json"


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


def build_release_handoff_artifacts(
    *,
    handoff_id: str | None = None,
    seal_path: str | Path = DEFAULT_SEAL_PATH,
    final_summary_path: str | Path = DEFAULT_FINAL_SUMMARY_PATH,
    audit_package_manifest_path: str | Path = DEFAULT_AUDIT_PACKAGE_MANIFEST_PATH,
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    gate_report_path: str | Path = DEFAULT_GATE_REPORT_PATH,
    signoff_summary_path: str | Path = DEFAULT_SIGNOFF_SUMMARY_PATH,
    handoff_manifest_path: str | Path = DEFAULT_HANDOFF_MANIFEST_PATH,
    prechange_template_path: str | Path = DEFAULT_PRECHANGE_TEMPLATE_PATH,
) -> Dict[str, Any]:
    current_handoff_id = str(handoff_id or f"handoff-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")

    seal_status, seal_reason, seal = _load_json_status(seal_path)
    final_status, final_reason, final_summary = _load_json_status(final_summary_path)
    package_status, package_reason, package = _load_json_status(audit_package_manifest_path)
    baseline_status, baseline_reason, baseline = _load_json_status(baseline_path)
    gate_status_raw, gate_reason, gate = _load_json_status(gate_report_path)
    signoff_status_raw, signoff_reason, signoff = _load_json_status(signoff_summary_path)

    required_paths = [
        str(seal_path),
        str(final_summary_path),
        str(audit_package_manifest_path),
        str(baseline_path),
        str(gate_report_path),
        str(signoff_summary_path),
    ]
    missing_required = [path for path in required_paths if not Path(path).exists()]

    baseline_digest = _sha256(Path(baseline_path)) if Path(baseline_path).exists() else ""
    known_blockers = [str(x) for x in _as_list(_as_dict(final_summary).get("known_blockers_or_risks")) if str(x)]
    if not known_blockers:
        known_blockers = [str(x) for x in _as_list(_as_dict(signoff).get("blockers")) if str(x)]

    upstream_states = {
        str(seal_path): {"status": seal_status, "reason": seal_reason},
        str(final_summary_path): {"status": final_status, "reason": final_reason},
        str(audit_package_manifest_path): {"status": package_status, "reason": package_reason},
        str(baseline_path): {"status": baseline_status, "reason": baseline_reason},
        str(gate_report_path): {"status": gate_status_raw, "reason": gate_reason},
        str(signoff_summary_path): {"status": signoff_status_raw, "reason": signoff_reason},
    }

    degraded_sources = [path for path, meta in upstream_states.items() if str(meta.get("status")) != "ok"]
    handoff_status = "ready"
    if missing_required or degraded_sources:
        handoff_status = "degraded"

    handoff_manifest = {
        "handoff_id": current_handoff_id,
        "created_at": _iso_now(),
        "sealed_release_tag": str(_as_dict(seal).get("release_tag") or ""),
        "baseline_digest": baseline_digest,
        "required_artifacts": [
            "release_seal",
            "release_final_delivery_summary",
            "release_audit_package_manifest",
            "research_safety_contract_freeze_baseline",
            "research_safety_contract_gate_report",
            "release_signoff_summary",
        ],
        "artifact_paths": {
            "release_seal": str(seal_path),
            "release_final_delivery_summary": str(final_summary_path),
            "release_audit_package_manifest": str(audit_package_manifest_path),
            "research_safety_contract_freeze_baseline": str(baseline_path),
            "research_safety_contract_gate_report": str(gate_report_path),
            "release_signoff_summary": str(signoff_summary_path),
        },
        "known_blockers": known_blockers if known_blockers else ["none"],
        "handoff_status": handoff_status,
        "upstream_status": upstream_states,
        "missing_required_artifacts": missing_required,
        "linked_package_id": str(_as_dict(package).get("package_id") or ""),
    }

    prechange_template = {
        "template_id": "release_prechange_checklist_v1",
        "created_at": _iso_now(),
        "must_check_freeze_baseline": True,
        "must_run_freeze_gate": True,
        "must_review_signoff_summary": True,
        "must_preserve_execution_guard": True,
        "must_not_change_overall_status_semantics": True,
        "must_update_baseline_if_contract_changes": True,
        "notes": [
            "checklist_only_no_automatic_execution",
            "change_must_be_validated_against_freeze_gate",
        ],
        "reference_paths": {
            "freeze_baseline": str(baseline_path),
            "freeze_gate_report": str(gate_report_path),
            "signoff_summary": str(signoff_summary_path),
            "handoff_manifest": str(handoff_manifest_path),
        },
    }

    handoff_file = Path(handoff_manifest_path)
    template_file = Path(prechange_template_path)
    handoff_file.parent.mkdir(parents=True, exist_ok=True)
    template_file.parent.mkdir(parents=True, exist_ok=True)
    handoff_file.write_text(json.dumps(handoff_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    template_file.write_text(json.dumps(prechange_template, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "release_handoff_manifest_json": str(handoff_file),
        "release_prechange_checklist_template_json": str(template_file),
        "release_handoff_manifest": handoff_manifest,
        "release_prechange_checklist_template": prechange_template,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate release handoff manifest and prechange checklist template")
    parser.add_argument("--handoff-id", default="")
    args = parser.parse_args()
    bundle = build_release_handoff_artifacts(handoff_id=str(args.handoff_id or "") or None)
    handoff = _as_dict(bundle.get("release_handoff_manifest"))
    print("release_handoff_manifest:", bundle.get("release_handoff_manifest_json"))
    print("release_prechange_checklist_template:", bundle.get("release_prechange_checklist_template_json"))
    print("handoff_status:", handoff.get("handoff_status"))


if __name__ == "__main__":
    main()

