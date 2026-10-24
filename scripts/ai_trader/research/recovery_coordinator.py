from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from scripts.ai_trader.research.ops_hardening_acceptance import load_json_safe, write_json_crash_safe


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


def build_recovery_coordinator_report(
    *,
    day_id: str,
    run_id: str | None = None,
    incident_index: Mapping[str, Any],
    trading_day_status: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "recovery_coordinator_report.json"

    index = _as_dict(incident_index)
    critical_count = int(index.get("critical_count") or 0)
    open_count = int(index.get("open_count") or 0)
    stage = str(_as_dict(trading_day_status).get("current_stage") or "unknown")
    actions: list[Dict[str, Any]] = []

    if critical_count > 0:
        actions.append({"action": "flatten_and_pause", "reason": "critical_incident_detected", "applied": True})
        actions.append({"action": "skip_unsafe_stages", "reason": "protect_capital", "applied": True})
        actions.append({"action": "safe_resume_next_day", "reason": "defer_to_next_day_safe_mode", "applied": True})
    elif open_count > 0:
        actions.append({"action": "retry_same_day_safe_stages", "reason": "non_critical_open_incidents", "applied": True})
    else:
        actions.append({"action": "continue_normal_schedule", "reason": "no_open_incidents", "applied": True})

    payload = {
        "day_id": day_id,
        "run_id": str(run_id or day_id),
        "current_stage": stage,
        "incident_summary": {
            "critical_count": critical_count,
            "open_count": open_count,
        },
        "actions": actions,
        "samples": {
            "flatten_and_pause": {"action": "flatten_and_pause", "reason": "critical_incident_detected"},
            "safe_resume_next_day": {"action": "safe_resume_next_day", "reason": "defer_to_next_day_safe_mode"},
        },
        "generated_at": _iso_now(),
    }
    existing = load_json_safe(path)
    signature = {
        "day_id": payload["day_id"],
        "run_id": payload["run_id"],
        "current_stage": payload["current_stage"],
        "incident_summary": payload["incident_summary"],
        "actions": payload["actions"],
    }
    existing_signature = {
        "day_id": str(existing.get("day_id") or ""),
        "run_id": str(existing.get("run_id") or ""),
        "current_stage": str(existing.get("current_stage") or ""),
        "incident_summary": _as_dict(existing.get("incident_summary")),
        "actions": _as_list(existing.get("actions")),
    }
    if existing_signature == signature:
        existing["idempotent_reuse"] = True
        write_json_crash_safe(path, existing)
        return {"recovery_coordinator_report_json": str(path), "recovery_coordinator_report": existing}
    write_json_crash_safe(path, payload)
    return {"recovery_coordinator_report_json": str(path), "recovery_coordinator_report": payload}
