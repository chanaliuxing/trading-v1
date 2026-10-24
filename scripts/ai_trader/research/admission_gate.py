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


def build_admission_gate(
    *,
    cycle_id: str,
    candidate_lifecycle_index: Mapping[str, Any],
    candidate_retention_report: Mapping[str, Any],
    risk_guard_report: Mapping[str, Any],
    min_cycles_observed: int = 1,
    min_shadow_runs: int = 1,
    max_avg_slippage_bps: float = 10.0,
    min_fill_rate: float = 0.75,
) -> Dict[str, Any]:
    lifecycle_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(candidate_lifecycle_index).get("candidates"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    retention_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(candidate_retention_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    risk_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(risk_guard_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }

    candidate_ids = sorted(set(list(lifecycle_by_id.keys()) + list(retention_by_id.keys()) + list(risk_by_id.keys())))
    items: list[Dict[str, Any]] = []
    for cid in candidate_ids:
        lifecycle = _as_dict(lifecycle_by_id.get(cid))
        retention = _as_dict(retention_by_id.get(cid))
        risk = _as_dict(risk_by_id.get(cid))
        cycles_observed = int(retention.get("cycles_observed") or 0)
        shadow_runs = int(retention.get("shadow_runs") or 0)
        avg_slippage = float(retention.get("avg_slippage_delta") or 0.0)
        fill_quality = float(retention.get("avg_fill_quality") or 0.0)
        approx_fill_rate = round(0.9 + fill_quality, 4)
        no_recent_kill_switch = not bool(risk.get("kill_switch"))
        no_open_risk_violations = bool(risk.get("allowed", True))
        reasons: list[str] = []
        approved = True

        if cycles_observed < min_cycles_observed:
            approved = False
            reasons.append("min_cycles_not_met")
        if shadow_runs < min_shadow_runs:
            approved = False
            reasons.append("min_shadow_runs_not_met")
        if avg_slippage > max_avg_slippage_bps:
            approved = False
            reasons.append("avg_slippage_too_high")
        if approx_fill_rate < min_fill_rate:
            approved = False
            reasons.append("fill_rate_too_low")
        if not no_recent_kill_switch:
            approved = False
            reasons.append("recent_kill_switch")
        if not no_open_risk_violations:
            approved = False
            reasons.append("open_risk_violations")

        items.append(
            {
                "candidate_id": cid,
                "experiment_id": str(lifecycle.get("experiment_id") or retention.get("experiment_id") or risk.get("experiment_id") or ""),
                "mode": (
                    "live"
                    if str(lifecycle.get("current_state") or "").endswith("_active")
                    else "paper"
                    if "candidate" in str(lifecycle.get("current_state") or "")
                    else "shadow"
                ),
                "admission_status": "approved" if approved else "rejected",
                "reasons": reasons or ["admission_passed"],
                "checks": {
                    "cycles_observed": cycles_observed,
                    "shadow_runs": shadow_runs,
                    "avg_slippage_bps": avg_slippage,
                    "fill_rate": approx_fill_rate,
                    "no_recent_kill_switch": no_recent_kill_switch,
                    "no_open_risk_violations": no_open_risk_violations,
                },
                "evaluated_at": _iso_now(),
            }
        )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": items,
        "summary": {
            "approved": sum(1 for x in items if x.get("admission_status") == "approved"),
            "rejected": sum(1 for x in items if x.get("admission_status") == "rejected"),
        },
    }
