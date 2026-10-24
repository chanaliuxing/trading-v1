from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from scripts.ai_trader.research.ops_hardening_acceptance import write_json_crash_safe


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


def build_operator_shift_summary(
    *,
    day_id: str,
    run_id: str | None = None,
    incident_index: Mapping[str, Any],
    active_registry: Mapping[str, Any],
    ops_control_center: Mapping[str, Any],
    recovery_coordinator_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "operator_shift_summary.json"

    incidents = _as_dict(incident_index)
    active_rows = [_as_dict(x) for x in _as_list(_as_dict(active_registry).get("items"))]
    ops = _as_dict(ops_control_center)
    recovery_actions = [_as_dict(x) for x in _as_list(_as_dict(recovery_coordinator_report).get("actions"))]

    open_incidents = int(incidents.get("open_count") or 0)
    critical_incidents = int(incidents.get("critical_count") or 0)
    active_candidates = sum(1 for x in active_rows if str(x.get("status") or "") == "active")
    paused_candidates = sum(1 for x in active_rows if str(x.get("status") or "") in {"inactive", "halted"})

    last_halt = ""
    last_flatten = ""
    for row in _as_list(incidents.get("items")):
        item = _as_dict(row)
        action = str(item.get("action_taken") or "")
        if "pause" in action and not last_halt:
            last_halt = str(item.get("incident_id") or "")
        if "flatten" in action and not last_flatten:
            last_flatten = str(item.get("incident_id") or "")

    payload = {
        "day_id": day_id,
        "run_id": str(run_id or day_id),
        "open_incidents": open_incidents,
        "critical_incidents": critical_incidents,
        "active_candidates": active_candidates,
        "paused_candidates": paused_candidates,
        "capital_in_use": float(ops.get("capital_in_use") or 0.0),
        "last_halt": last_halt,
        "last_flatten": last_flatten,
        "next_actions": [str(x.get("action") or "") for x in recovery_actions] or ["continue_monitoring"],
        "samples": {
            "open_incidents_shift": {"open_incidents": max(open_incidents, 1), "critical_incidents": max(critical_incidents, 1)},
            "clean_shift": {"open_incidents": 0, "critical_incidents": 0},
        },
        "generated_at": _iso_now(),
    }
    write_json_crash_safe(path, payload)
    return {"operator_shift_summary_json": str(path), "operator_shift_summary": payload}
