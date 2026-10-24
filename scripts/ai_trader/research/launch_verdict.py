from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.policy_compatibility_observability import (
    DEFAULT_POLICY_COMPATIBILITY_SUMMARY_PATH,
    DEFAULT_POLICY_OBSERVABILITY_CONSUMERS_PATH,
    build_legacy_policy_compatibility,
    build_policy_compatibility_observability,
)


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


def _build_policy_compatibility_observability_view(
    *,
    readiness_dashboard: Mapping[str, Any],
    policy_compatibility_summary_path: str | Path,
    policy_observability_consumers_path: str | Path,
) -> Dict[str, Any]:
    dashboard_obs = _as_dict(_as_dict(readiness_dashboard).get("policy_compatibility_observability"))
    if dashboard_obs:
        return dashboard_obs
    return build_policy_compatibility_observability(
        policy_compatibility_summary_path,
        consumers_inventory_path=policy_observability_consumers_path,
    )


def build_launch_verdict(
    *,
    cycle_id: str,
    preflight_check: Mapping[str, Any],
    launch_checklist: Mapping[str, Any],
    readiness_dashboard: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
    policy_compatibility_summary_path: str | Path = DEFAULT_POLICY_COMPATIBILITY_SUMMARY_PATH,
    policy_observability_consumers_path: str | Path = DEFAULT_POLICY_OBSERVABILITY_CONSUMERS_PATH,
) -> Dict[str, Any]:
    preflight = _as_dict(preflight_check)
    checklist = _as_dict(launch_checklist)
    dashboard = _as_dict(readiness_dashboard)
    admission = _as_dict(admission_gate)

    paper_ready = bool(dashboard.get("paper_ready"))
    live_ready = bool(dashboard.get("live_ready"))
    blocked_issues = [str(x) for x in _as_list(preflight.get("blocking_issues"))]
    overall_status = str(preflight.get("overall_status") or "blocked")
    policy_compatibility_observability = _build_policy_compatibility_observability_view(
        readiness_dashboard=dashboard,
        policy_compatibility_summary_path=policy_compatibility_summary_path,
        policy_observability_consumers_path=policy_observability_consumers_path,
    )
    policy_compatibility = build_legacy_policy_compatibility(policy_compatibility_observability)
    steps = _as_list(checklist.get("steps"))

    live_step_blockers = [
        _as_dict(s)
        for s in steps
        if str(_as_dict(s).get("launch_mode") or "") == "small_live" and str(_as_dict(s).get("status") or "") == "blocked"
    ]

    operator_status = _check_status(preflight, "operator_controls")
    if live_ready and overall_status == "ready":
        verdict = "go_small_live"
    elif paper_ready:
        if operator_status == "blocked" or live_step_blockers:
            verdict = "hold_live"
        else:
            verdict = "go_paper"
    else:
        verdict = "hold_paper"

    admission_items = [_as_dict(x) for x in _as_list(admission.get("items"))]
    approved_candidates = [str(x.get("candidate_id") or "") for x in admission_items if str(x.get("admission_status") or "") == "approved"]
    rejected_candidates = [str(x.get("candidate_id") or "") for x in admission_items if str(x.get("admission_status") or "") != "approved"]

    reason_summary: list[str] = []
    if verdict == "go_small_live":
        reason_summary.append("live_ready_and_all_preflight_checks_ready")
    elif verdict == "go_paper":
        reason_summary.append("paper_ready_no_blocking_issue")
    elif verdict == "hold_live":
        reason_summary.append("paper_ready_but_live_controls_not_ready")
    else:
        reason_summary.append("paper_not_ready_or_preflight_blocked")

    if blocked_issues:
        reason_summary.extend(blocked_issues[:3])
    elif live_step_blockers:
        reason_summary.append("live_checklist_has_pending_or_blocked_steps")

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "verdict": verdict,
        "reason_summary": reason_summary,
        "blocking_issues": blocked_issues,
        "approved_candidates": approved_candidates,
        "rejected_candidates": rejected_candidates,
        "policy_compatibility": policy_compatibility,
        "policy_compatibility_observability": policy_compatibility_observability,
        "evidence_refs": {
            "preflight": "preflight_check.json",
            "checklist": "launch_checklist.json",
            "dashboard": "readiness_dashboard.json",
        },
    }
