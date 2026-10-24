from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from scripts.ai_trader.research.ops_hardening_acceptance import write_json_crash_safe


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"


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


def update_ops_registry(
    *,
    day_id: str,
    run_id: str | None = None,
    system_mode: str,
    trading_day_status: Mapping[str, Any],
    launch_status: Mapping[str, Any],
    ops_control_center: Mapping[str, Any],
    incident_index: Mapping[str, Any],
    reliability_metrics: Mapping[str, Any],
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> Dict[str, Any]:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ops_registry.json"

    payload = {
        "last_cycle_id": day_id,
        "run_id": str(run_id or day_id),
        "system_mode": str(system_mode or "paper"),
        "active_batches": _as_list(_as_dict(ops_control_center).get("active_batches")),
        "active_candidates": _as_list(_as_dict(ops_control_center).get("active_candidates")),
        "open_incidents": int(_as_dict(incident_index).get("open_count") or 0),
        "current_day_status": str(_as_dict(trading_day_status).get("current_stage") or "unknown"),
        "launch_status": str(_as_dict(launch_status).get("verdict") or "hold_paper"),
        "reliability_status": str(_as_dict(reliability_metrics).get("status") or "unknown"),
        "updated_at": _iso_now(),
        "incident_and_recovery_sync": {
            "critical_incidents": int(_as_dict(incident_index).get("critical_count") or 0),
            "recovery_mode": "safe" if str(_as_dict(reliability_metrics).get("status") or "") == "degraded" else "normal",
        },
    }
    write_json_crash_safe(path, payload)
    return {"ops_registry_json": str(path), "ops_registry": payload}
