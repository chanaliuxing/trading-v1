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


def build_ops_control_center(
    *,
    day_id: str,
    live_batch_plan: Mapping[str, Any],
    live_batch_status: Mapping[str, Any],
    intraday_batch_report: Mapping[str, Any],
    live_ops_summary: Mapping[str, Any],
    capital_governor_report: Mapping[str, Any],
    operator_controls: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "ops_control_center.json"

    plan = _as_dict(live_batch_plan)
    status = _as_dict(live_batch_status)
    intra = _as_dict(intraday_batch_report)
    summary = _as_dict(live_ops_summary)
    capital = _as_dict(capital_governor_report)
    controls = _as_dict(operator_controls)

    warnings = list(_as_list(intra.get("batch_warnings")))
    halts = [str(_as_dict(x).get("candidate_id") or "") for x in _as_list(intra.get("items")) if "halt_batch" in _as_list(_as_dict(x).get("actions")) or "pause_one" in _as_list(_as_dict(x).get("actions"))]
    flatten_actions = [str(_as_dict(x).get("candidate_id") or "") for x in _as_list(intra.get("items")) if "flatten_one" in _as_list(_as_dict(x).get("actions"))]
    capital_in_use = sum(float(_as_dict(x).get("effective_capital") or 0.0) for x in _as_list(capital.get("items")))

    payload = {
        "day_id": day_id,
        "system_mode": str(plan.get("mode") or "paper"),
        "active_batches": [str(plan.get("batch_id") or "")],
        "active_candidates": list(_as_list(summary.get("launched_candidates"))),
        "warnings": warnings,
        "halts": halts,
        "flatten_actions": flatten_actions,
        "capital_in_use": round(capital_in_use, 2),
        "live_enabled": bool(controls.get("live_enabled", False)),
        "next_required_actions": list(_as_list(summary.get("next_actions"))) or ["review_ops_center_alerts"],
        "samples": {
            "normal_running": {"warnings": [], "halts": [], "flatten_actions": []},
            "halt_warning_case": {"warnings": ["batch_latency_warning"], "halts": ["sample-halt-candidate"]},
        },
        "generated_at": _iso_now(),
        "batch_status": str(status.get("batch_status") or ""),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ops_control_center_json": str(path), "ops_control_center": payload}

