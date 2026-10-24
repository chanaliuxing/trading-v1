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


def _check_status(preflight: Mapping[str, Any], name: str) -> str:
    for row in _as_list(_as_dict(preflight).get("checks")):
        item = _as_dict(row)
        if str(item.get("name") or "") == name:
            return str(item.get("status") or "unknown")
    return "unknown"


def _step_status(*, check_status: str, required: bool) -> str:
    if check_status == "blocked":
        return "blocked" if required else "pending"
    if check_status == "degraded":
        return "pending"
    return "done"


def build_launch_checklist(
    *,
    cycle_id: str,
    preflight_check: Mapping[str, Any],
    readiness_dashboard: Mapping[str, Any],
) -> Dict[str, Any]:
    paper_ready = bool(_as_dict(readiness_dashboard).get("paper_ready"))
    live_ready = bool(_as_dict(readiness_dashboard).get("live_ready"))

    op_status = _check_status(preflight_check, "operator_controls")
    cap_status = _check_status(preflight_check, "capital_governor")
    session_status = _check_status(preflight_check, "session_control")
    admission_status = _check_status(preflight_check, "admission_gate")
    health_status = _check_status(preflight_check, "health_monitor")
    artifacts_status = _check_status(preflight_check, "required_artifacts")

    steps = [
        {
            "step_id": "paper-1",
            "step_name": "paper_admission_gate",
            "launch_mode": "paper",
            "status": _step_status(check_status=admission_status, required=True),
            "required": True,
            "owner": "admission",
            "evidence_ref": "admission_gate.json",
        },
        {
            "step_id": "paper-2",
            "step_name": "paper_health_green",
            "launch_mode": "paper",
            "status": _step_status(check_status=health_status, required=True),
            "required": True,
            "owner": "health",
            "evidence_ref": "health_monitor_report.json",
        },
        {
            "step_id": "paper-3",
            "step_name": "paper_artifacts_present",
            "launch_mode": "paper",
            "status": _step_status(check_status=artifacts_status, required=True),
            "required": True,
            "owner": "orchestrator",
            "evidence_ref": "preflight_check.json",
        },
        {
            "step_id": "live-1",
            "step_name": "live_operator_controls",
            "launch_mode": "small_live",
            "status": _step_status(check_status=op_status, required=True),
            "required": True,
            "owner": "operator",
            "evidence_ref": "operator_controls.json",
        },
        {
            "step_id": "live-2",
            "step_name": "live_capital_limits",
            "launch_mode": "small_live",
            "status": _step_status(check_status=cap_status, required=True),
            "required": True,
            "owner": "risk",
            "evidence_ref": "capital_governor_report.json",
        },
        {
            "step_id": "live-3",
            "step_name": "live_session_open",
            "launch_mode": "small_live",
            "status": _step_status(check_status=session_status, required=True),
            "required": True,
            "owner": "session",
            "evidence_ref": "session_control_report.json",
        },
    ]
    if paper_ready:
        steps.append(
            {
                "step_id": "paper-4",
                "step_name": "paper_launch_ready",
                "launch_mode": "paper",
                "status": "done",
                "required": True,
                "owner": "director",
                "evidence_ref": "readiness_dashboard.json",
            }
        )
    if live_ready:
        steps.append(
            {
                "step_id": "live-4",
                "step_name": "live_launch_ready",
                "launch_mode": "small_live",
                "status": "done",
                "required": True,
                "owner": "director",
                "evidence_ref": "readiness_dashboard.json",
            }
        )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "steps": steps,
        "summary": {
            "paper_required": sum(1 for s in steps if s.get("launch_mode") == "paper" and s.get("required")),
            "live_required": sum(1 for s in steps if s.get("launch_mode") == "small_live" and s.get("required")),
            "blocked_steps": sum(1 for s in steps if s.get("status") == "blocked"),
            "pending_steps": sum(1 for s in steps if s.get("status") == "pending"),
            "ready_paper": paper_ready,
            "ready_live": live_ready,
        },
    }
