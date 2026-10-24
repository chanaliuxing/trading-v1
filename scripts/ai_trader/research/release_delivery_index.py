from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_BASELINE_PATH = "docs/research_safety_contract_freeze_baseline.json"
DEFAULT_SEAL_PATH = "runs/ai_trader/research/state/release_seal.json"
DEFAULT_FINAL_SUMMARY_PATH = "runs/ai_trader/research/state/release_final_delivery_summary.json"
DEFAULT_PACKAGE_MANIFEST_PATH = "runs/ai_trader/research/state/release_audit_package_manifest.json"
DEFAULT_HANDOFF_MANIFEST_PATH = "runs/ai_trader/research/state/release_handoff_manifest.json"
DEFAULT_PRECHANGE_TEMPLATE_PATH = "runs/ai_trader/research/state/release_prechange_checklist_template.json"
DEFAULT_SIGNOFF_SUMMARY_PATH = "runs/ai_trader/research/state/release_signoff_summary.json"
DEFAULT_DELIVERY_INDEX_PATH = "runs/ai_trader/research/state/release_delivery_index.json"


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


def build_release_delivery_index(
    *,
    index_id: str | None = None,
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    seal_path: str | Path = DEFAULT_SEAL_PATH,
    final_summary_path: str | Path = DEFAULT_FINAL_SUMMARY_PATH,
    package_manifest_path: str | Path = DEFAULT_PACKAGE_MANIFEST_PATH,
    handoff_manifest_path: str | Path = DEFAULT_HANDOFF_MANIFEST_PATH,
    prechange_template_path: str | Path = DEFAULT_PRECHANGE_TEMPLATE_PATH,
    signoff_summary_path: str | Path = DEFAULT_SIGNOFF_SUMMARY_PATH,
    output_path: str | Path = DEFAULT_DELIVERY_INDEX_PATH,
) -> Dict[str, Any]:
    current_index_id = str(index_id or f"delivery-index-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")

    baseline_status, baseline_reason, baseline = _load_json_status(baseline_path)
    seal_status, seal_reason, seal = _load_json_status(seal_path)
    final_status, final_reason, final_summary = _load_json_status(final_summary_path)
    package_status, package_reason, package = _load_json_status(package_manifest_path)
    handoff_status_raw, handoff_reason, handoff = _load_json_status(handoff_manifest_path)
    template_status, template_reason, template = _load_json_status(prechange_template_path)
    signoff_status_raw, signoff_reason, signoff = _load_json_status(signoff_summary_path)

    baseline_file = Path(baseline_path)
    baseline_digest = _sha256(baseline_file) if baseline_file.exists() else ""
    sealed_release_tag = str(_as_dict(seal).get("release_tag") or "")

    key_artifacts = [
        {"name": "freeze_baseline", "path": str(baseline_path), "role": "frozen_contract_source"},
        {"name": "release_seal", "path": str(seal_path), "role": "sealed_release_marker"},
        {"name": "release_final_delivery_summary", "path": str(final_summary_path), "role": "final_delivery_snapshot"},
        {"name": "release_audit_package_manifest", "path": str(package_manifest_path), "role": "audit_snapshot_manifest"},
        {"name": "release_handoff_manifest", "path": str(handoff_manifest_path), "role": "handoff_inventory"},
        {"name": "release_prechange_checklist_template", "path": str(prechange_template_path), "role": "prechange_control_template"},
        {"name": "release_signoff_summary", "path": str(signoff_summary_path), "role": "signoff_state"},
    ]

    required_commands = [
        "venv/bin/python -m scripts.ai_trader.research.run_fixture_cycle",
        "venv/bin/python -m scripts.ai_trader.research.freeze_change_gate",
        "venv/bin/python -m scripts.ai_trader.research.release_signoff_summary",
        "venv/bin/python -m scripts.ai_trader.research.candidate_batch_record --batch-id <batch_id> --run-id <run_id> --cycle-id fixture-2026-04-05",
        "venv/bin/python -m scripts.ai_trader.research.candidate_batch_audit_summary --keep-last-records 10",
        "venv/bin/python -m scripts.ai_trader.research.candidate_batch_query --export-path runs/ai_trader/research/state/release_candidate_batch_query_export.json",
    ]

    known_blockers = [str(x) for x in _as_list(_as_dict(final_summary).get("known_blockers_or_risks")) if str(x)]
    if not known_blockers:
        known_blockers = [str(x) for x in _as_list(_as_dict(signoff).get("blockers")) if str(x)]
    if not known_blockers:
        known_blockers = ["none"]

    upstream_status = {
        str(baseline_path): {"status": baseline_status, "reason": baseline_reason},
        str(seal_path): {"status": seal_status, "reason": seal_reason},
        str(final_summary_path): {"status": final_status, "reason": final_reason},
        str(package_manifest_path): {"status": package_status, "reason": package_reason},
        str(handoff_manifest_path): {"status": handoff_status_raw, "reason": handoff_reason},
        str(prechange_template_path): {"status": template_status, "reason": template_reason},
        str(signoff_summary_path): {"status": signoff_status_raw, "reason": signoff_reason},
    }
    degraded = [path for path, meta in upstream_status.items() if str(meta.get("status")) != "ok"]
    status_text = "ok" if not degraded else "degraded"

    final_status_summary = {
        "gate_status": str(_as_dict(seal).get("gate_status") or _as_dict(_as_dict(package).get("gate_status")) or "hold"),
        "signoff_status": str(_as_dict(seal).get("signoff_status") or ("ready" if bool(_as_dict(signoff).get("signoff_ready", False)) else "not_ready")),
        "handoff_status": str(_as_dict(handoff).get("handoff_status") or "degraded"),
        "index_status": status_text,
    }

    index_payload = {
        "index_id": current_index_id,
        "created_at": _iso_now(),
        "sealed_release_tag": sealed_release_tag,
        "baseline_digest": baseline_digest,
        "final_status_summary": final_status_summary,
        "key_artifacts": key_artifacts,
        "required_commands": required_commands,
        "known_blockers_or_risks": known_blockers,
        "handoff_paths": {
            "release_handoff_manifest": str(handoff_manifest_path),
            "release_prechange_checklist_template": str(prechange_template_path),
            "release_seal": str(seal_path),
            "release_final_delivery_summary": str(final_summary_path),
        },
        "upstream_status": upstream_status,
        "status": status_text,
        "notes": [
            "single_page_index_for_phase_26_36_handoff",
            "future_changes_must_reference_prechange_checklist_template",
        ],
    }

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "release_delivery_index_json": str(out),
        "release_delivery_index": index_payload,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build phase 26-36 release delivery index")
    parser.add_argument("--index-id", default="")
    args = parser.parse_args()
    bundle = build_release_delivery_index(index_id=str(args.index_id or "") or None)
    payload = _as_dict(bundle.get("release_delivery_index"))
    print("release_delivery_index:", bundle.get("release_delivery_index_json"))
    print("index_id:", payload.get("index_id"))
    print("status:", payload.get("status"))


if __name__ == "__main__":
    main()

