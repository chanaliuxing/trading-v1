from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


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


def build_canary_launch_gate(
    *,
    cycle_id: str,
    candidate_id: str,
    launch_verdict: Mapping[str, Any],
    broker_safety_gate: Mapping[str, Any],
    broker_launch_barrier: Mapping[str, Any],
    operator_controls: Mapping[str, Any],
    broker_launch_switch: Mapping[str, Any],
    manual_approval_report: Mapping[str, Any],
    candidate_lifecycle_index: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    candidate_state = ""
    for row in _as_list(_as_dict(candidate_lifecycle_index).get("candidates")):
        item = _as_dict(row)
        if str(item.get("candidate_id") or "") == str(candidate_id):
            candidate_state = str(item.get("current_state") or "")
            break

    checks = {
        "launch_verdict_go_small_live": str(_as_dict(launch_verdict).get("verdict") or "") == "go_small_live",
        "broker_safety_gate_pass": str(_as_dict(broker_safety_gate).get("gate_status") or "") in {"approved", "not_applicable"},
        "launch_barrier_pass": str(_as_dict(broker_launch_barrier).get("barrier_status") or "") == "passed",
        "operator_live_enabled": bool(_as_dict(operator_controls).get("live_enabled", False)),
        "paper_only_false": not bool(_as_dict(operator_controls).get("paper_only", True)),
        "real_broker_enabled_true": bool(_as_dict(broker_launch_switch).get("real_broker_enabled", False)),
        "real_broker_dry_run_only_false": not bool(_as_dict(broker_launch_switch).get("real_broker_dry_run_only", True)),
        "manual_approval_approved": str(_as_dict(manual_approval_report).get("status") or "") == "approved",
        "candidate_lifecycle_allowed": candidate_state in {"paper_active", "shadow_active", "live_candidate"},
    }
    reasons = [k for k, v in checks.items() if not v]
    gate_status = "approved" if not reasons else "rejected"

    approved_example = {
        "candidate_id": candidate_id,
        "gate_status": "approved",
        "reasons": ["all_conditions_met"],
    }
    rejected_example = {
        "candidate_id": f"{candidate_id}-blocked",
        "gate_status": "rejected",
        "reasons": ["manual_approval_missing"],
    }

    payload = {
        "cycle_id": cycle_id,
        "candidate_id": candidate_id,
        "candidate_state": candidate_state,
        "gate_status": gate_status,
        "checks": checks,
        "reasons": reasons or ["all_conditions_met"],
        "approved_example": approved_example,
        "rejected_example": rejected_example,
        "generated_at": _iso_now(),
    }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "canary_launch_gate.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"canary_launch_gate_json": str(path), "canary_launch_gate": payload}

