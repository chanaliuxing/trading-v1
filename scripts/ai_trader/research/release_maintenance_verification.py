from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.freeze_change_gate import (
    DEFAULT_GATE_REPORT_PATH,
    evaluate_freeze_change_gate,
)
from scripts.ai_trader.research.release_delivery_index import (
    DEFAULT_DELIVERY_INDEX_PATH,
    build_release_delivery_index,
)
from scripts.ai_trader.research.release_handoff import (
    DEFAULT_PRECHANGE_TEMPLATE_PATH,
)
from scripts.ai_trader.research.release_signoff_summary import (
    DEFAULT_SIGNOFF_SUMMARY_PATH,
    build_release_signoff_summary,
)


DEFAULT_MAINTENANCE_VERIFICATION_PATH = "runs/ai_trader/research/state/release_maintenance_verifications.json"
DEFAULT_MAINTENANCE_MODE_STATE_PATH = "runs/ai_trader/research/state/release_maintenance_mode_state.json"

MAINTENANCE_REQUIRED_SEQUENCE = [
    "test -f runs/ai_trader/research/state/release_prechange_checklist_template.json",
    "venv/bin/python -m scripts.ai_trader.research.freeze_change_gate",
    "venv/bin/python -m scripts.ai_trader.research.release_signoff_summary",
    "venv/bin/python -m scripts.ai_trader.research.release_delivery_index --index-id <index_id>",
    "venv/bin/python -m scripts.ai_trader.research.release_maintenance_verification --verification-id <verification_id>",
]


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


def run_maintenance_verification(
    *,
    verification_id: str | None = None,
    output_path: str | Path = DEFAULT_MAINTENANCE_VERIFICATION_PATH,
    mode_state_path: str | Path = DEFAULT_MAINTENANCE_MODE_STATE_PATH,
) -> Dict[str, Any]:
    current_id = str(verification_id or f"maintenance-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")

    template_path = Path(DEFAULT_PRECHANGE_TEMPLATE_PATH)
    template_exists = template_path.exists()

    gate = evaluate_freeze_change_gate()
    signoff_bundle = build_release_signoff_summary()
    signoff = _as_dict(signoff_bundle.get("release_signoff_summary"))
    index_bundle = build_release_delivery_index()
    index = _as_dict(index_bundle.get("release_delivery_index"))

    freeze_gate_status = str(gate.get("gate_status") or "hold")
    signoff_status = str(signoff.get("signoff_status") or "not_ready")
    delivery_index_status = str(index.get("status") or "degraded")

    key_paths: list[str] = [
        str(DEFAULT_PRECHANGE_TEMPLATE_PATH),
        str(DEFAULT_GATE_REPORT_PATH),
        str(DEFAULT_SIGNOFF_SUMMARY_PATH),
        str(DEFAULT_DELIVERY_INDEX_PATH),
    ]
    key_paths.extend(
        [str(_as_dict(row).get("path") or "") for row in _as_list(index.get("key_artifacts")) if str(_as_dict(row).get("path") or "")]
    )
    unique_paths = []
    for path_text in key_paths:
        if path_text and path_text not in unique_paths:
            unique_paths.append(path_text)
    missing_paths = [path_text for path_text in unique_paths if not Path(path_text).exists()]

    notes = [
        "maintenance_mode_no_code_change_verification",
        f"template_exists={template_exists}",
    ]
    if missing_paths:
        notes.append("missing_required_paths_detected")
    if freeze_gate_status != "pass":
        notes.append("freeze_gate_not_pass")

    entry = {
        "verification_id": current_id,
        "checked_at": _iso_now(),
        "freeze_gate_status": freeze_gate_status,
        "signoff_status": signoff_status,
        "delivery_index_status": delivery_index_status,
        "notes": notes,
        "path_integrity": {
            "missing_paths": missing_paths,
            "required_paths": unique_paths,
        },
    }

    target = Path(output_path)
    existing = _load_json(target)
    records = [_as_dict(x) for x in _as_list(existing.get("records"))]
    records.append(entry)
    payload = {
        "records": records,
        "latest": entry,
        "updated_at": _iso_now(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    mode_state = {
        "maintenance_mode": True,
        "baseline_confirmed": True,
        "required_sequence": list(MAINTENANCE_REQUIRED_SEQUENCE),
        "last_verification_id": str(entry.get("verification_id") or ""),
        "last_verified_at": str(entry.get("checked_at") or ""),
        "notes": [
            "long_term_maintenance_mode_active",
            "no_feature_expansion_without_real_change_request",
        ],
    }
    mode_target = Path(mode_state_path)
    mode_target.parent.mkdir(parents=True, exist_ok=True)
    mode_target.write_text(json.dumps(mode_state, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "release_maintenance_verifications_json": str(target),
        "release_maintenance_mode_state_json": str(mode_target),
        "entry": entry,
        "mode_state": mode_state,
        "records_count": len(records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run maintenance mode baseline verification and append record")
    parser.add_argument("--verification-id", default="")
    args = parser.parse_args()
    result = run_maintenance_verification(verification_id=str(args.verification_id or "") or None)
    print("release_maintenance_verifications:", result.get("release_maintenance_verifications_json"))
    print("verification_id:", _as_dict(result.get("entry")).get("verification_id"))
    print("freeze_gate_status:", _as_dict(result.get("entry")).get("freeze_gate_status"))


if __name__ == "__main__":
    main()
