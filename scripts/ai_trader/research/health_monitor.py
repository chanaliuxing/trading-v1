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


def build_health_monitor_report(
    *,
    cycle_id: str,
    live_execution_report: Mapping[str, Any],
    live_feedback: Mapping[str, Any],
    risk_guard_report: Mapping[str, Any],
) -> Dict[str, Any]:
    exec_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(live_execution_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    fb_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(live_feedback).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    risk_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(risk_guard_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    candidate_ids = sorted(set(list(exec_by_id.keys()) + list(fb_by_id.keys()) + list(risk_by_id.keys())))

    items: list[Dict[str, Any]] = []
    for cid in candidate_ids:
        ex = _as_dict(exec_by_id.get(cid))
        fb = _as_dict(fb_by_id.get(cid))
        rg = _as_dict(risk_by_id.get(cid))
        fill_rate = float(ex.get("fill_rate") or 0.0)
        slippage = float(ex.get("avg_slippage_bps") or 0.0)
        latency = float(ex.get("avg_latency_ms") or 0.0)
        realized_pnl = float(fb.get("realized_pnl") or 0.0)
        risk_events = _as_list(fb.get("risk_events"))
        warnings: list[str] = []
        critical: list[str] = []

        if fill_rate < 0.85:
            warnings.append("fill_rate_drop")
        if slippage > 7.0:
            warnings.append("slippage_spike")
        if latency > 500:
            warnings.append("latency_spike")
        if realized_pnl < -50:
            critical.append("pnl_drawdown")
        if len(risk_events) >= 2:
            critical.append("repeated_risk_events")
        if bool(rg.get("kill_switch")):
            critical.append("kill_switch_triggered")
        if str(ex.get("execution_status") or "") == "blocked_by_controls" and not critical:
            warnings.append("execution_blocked")

        status = "critical" if critical else "warning" if warnings else "healthy"
        items.append(
            {
                "candidate_id": cid,
                "experiment_id": str(ex.get("experiment_id") or fb.get("experiment_id") or rg.get("experiment_id") or ""),
                "health_status": status,
                "warnings": sorted(set(warnings)),
                "critical": sorted(set(critical)),
                "metrics": {
                    "fill_rate": fill_rate,
                    "slippage_bps": slippage,
                    "latency_ms": latency,
                    "realized_pnl": realized_pnl,
                    "risk_events": risk_events,
                },
                "evaluated_at": _iso_now(),
            }
        )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": items,
        "summary": {
            "healthy": sum(1 for x in items if x.get("health_status") == "healthy"),
            "warning": sum(1 for x in items if x.get("health_status") == "warning"),
            "critical": sum(1 for x in items if x.get("health_status") == "critical"),
        },
    }
