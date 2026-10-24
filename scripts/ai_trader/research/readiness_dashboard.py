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


def build_readiness_dashboard(
    *,
    cycle_id: str,
    preflight_check: Mapping[str, Any],
    admission_gate: Mapping[str, Any],
    health_monitor_report: Mapping[str, Any],
    active_registry: Mapping[str, Any],
    policy_compatibility_summary_path: str | Path = DEFAULT_POLICY_COMPATIBILITY_SUMMARY_PATH,
    policy_observability_consumers_path: str | Path = DEFAULT_POLICY_OBSERVABILITY_CONSUMERS_PATH,
) -> Dict[str, Any]:
    admission_summary = _as_dict(_as_dict(admission_gate).get("summary"))
    health_summary = _as_dict(_as_dict(health_monitor_report).get("summary"))
    registry_items = _as_list(_as_dict(active_registry).get("items"))
    registry_summary = _as_dict(_as_dict(active_registry).get("summary"))

    approved = int(admission_summary.get("approved") or 0)
    critical = int(health_summary.get("critical") or 0)
    blocked_candidates = int(registry_summary.get("inactive") or 0) + int(registry_summary.get("halted") or 0)
    active_candidates = int(registry_summary.get("active") or 0)

    operator_status = _check_status(preflight_check, "operator_controls")
    session_status = _check_status(preflight_check, "session_control")
    capital_status = _check_status(preflight_check, "capital_governor")
    artifact_status = _check_status(preflight_check, "required_artifacts")
    policy_compatibility_observability = build_policy_compatibility_observability(
        policy_compatibility_summary_path,
        consumers_inventory_path=policy_observability_consumers_path,
    )
    policy_compatibility = build_legacy_policy_compatibility(policy_compatibility_observability)

    paper_ready = approved > 0 and critical == 0 and artifact_status != "blocked"
    live_ready = (
        paper_ready
        and operator_status == "ready"
        and session_status == "ready"
        and capital_status in {"ready", "degraded"}
        and active_candidates > 0
    )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "paper_ready": bool(paper_ready),
        "live_ready": bool(live_ready),
        "active_candidates": active_candidates,
        "blocked_candidates": max(blocked_candidates, len(registry_items) - active_candidates),
        "critical_alerts": critical,
        "policy_compatibility": policy_compatibility,
        "policy_compatibility_observability": policy_compatibility_observability,
        "last_cycle_id": cycle_id,
        "last_updated_at": _iso_now(),
    }
