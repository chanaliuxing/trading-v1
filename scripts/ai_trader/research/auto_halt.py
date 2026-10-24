from __future__ import annotations

from datetime import datetime, timezone
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


def build_auto_halt_report(
    *,
    cycle_id: str,
    health_monitor_report: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
) -> Dict[str, Any]:
    health_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(health_monitor_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    admission_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(admission_gate).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    ids = sorted(set(list(health_by_id.keys()) + list(admission_by_id.keys())))
    actions: list[Dict[str, Any]] = []
    critical_count = 0
    for cid in ids:
        h = _as_dict(health_by_id.get(cid))
        a = _as_dict(admission_by_id.get(cid))
        status = str(h.get("health_status") or "healthy")
        admission_status = str(a.get("admission_status") or "rejected")
        if status == "critical":
            critical_count += 1
            action = "pause_candidate"
            reason = "health_critical"
            if "kill_switch_triggered" in _as_list(h.get("critical")):
                action = "flatten_candidate"
                reason = "kill_switch_triggered"
            if admission_status == "rejected":
                action = "demote_candidate"
                reason = "admission_rejected_with_critical_health"
            actions.append(
                {
                    "candidate_id": cid,
                    "experiment_id": str(h.get("experiment_id") or a.get("experiment_id") or ""),
                    "action": action,
                    "reason": reason,
                    "source_health": status,
                    "admission_status": admission_status,
                    "ts": _iso_now(),
                }
            )
    if critical_count >= 2:
        actions.append(
            {
                "candidate_id": "*",
                "experiment_id": "",
                "action": "pause_live_globally",
                "reason": "multiple_critical_candidates",
                "source_health": "critical",
                "admission_status": "n/a",
                "ts": _iso_now(),
            }
        )
    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": actions,
        "summary": {
            "auto_halt_actions": len(actions),
            "demote_count": sum(1 for x in actions if x.get("action") == "demote_candidate"),
            "pause_count": sum(1 for x in actions if x.get("action") == "pause_candidate"),
            "flatten_count": sum(1 for x in actions if x.get("action") == "flatten_candidate"),
            "global_pause_count": sum(1 for x in actions if x.get("action") == "pause_live_globally"),
        },
    }
