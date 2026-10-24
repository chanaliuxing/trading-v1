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


def apply_canary_feedback(
    *,
    cycle_id: str,
    candidate_id: str,
    post_trade_attribution: Mapping[str, Any],
    intraday_monitor_report: Mapping[str, Any],
    candidate_lifecycle_index: Mapping[str, Any],
    active_registry: Mapping[str, Any],
    launch_registry: Mapping[str, Any],
    system_status: Mapping[str, Any],
    state_root: str | Path,
    output_dir: str | Path,
) -> Dict[str, Any]:
    attribution = _as_dict(post_trade_attribution)
    monitor = _as_dict(intraday_monitor_report)
    lifecycle = _as_dict(candidate_lifecycle_index)
    registry = _as_dict(active_registry)
    lreg = _as_dict(launch_registry)
    sstat = _as_dict(system_status)

    critical = _as_list(monitor.get("critical"))
    execution_dragged = bool(attribution.get("execution_dragged_result", False))
    if "repeated_rejects" in critical or "pnl_drawdown" in critical:
        action = "demote_or_pause"
        feedback_status = "risk_event"
        next_action = "pause_live_candidate"
    elif execution_dragged:
        action = "tighten_constraints"
        feedback_status = "execution_bad"
        next_action = "tighten_constraints"
    else:
        action = "keep_or_promote"
        feedback_status = "canary_ok"
        next_action = "keep_candidate"

    lifecycle_rows = [_as_dict(x) for x in _as_list(lifecycle.get("candidates"))]
    for row in lifecycle_rows:
        if str(row.get("candidate_id") or "") != str(candidate_id):
            continue
        if action == "keep_or_promote":
            row["current_state"] = "paper_active"
        else:
            row["current_state"] = "demoted"
        row["last_evaluated_at"] = _iso_now()
        row["last_transition_reason"] = [action]
    lifecycle["candidates"] = lifecycle_rows
    lifecycle["updated_at"] = _iso_now()

    reg_rows = [_as_dict(x) for x in _as_list(registry.get("items"))]
    for row in reg_rows:
        if str(row.get("candidate_id") or "") != str(candidate_id):
            continue
        if action == "keep_or_promote":
            row["status"] = "active"
            row["last_action"] = "canary_ok_keep"
        else:
            row["status"] = "inactive"
            row["last_action"] = "canary_pause"
    registry["items"] = reg_rows
    registry["updated_at"] = _iso_now()

    lreg["updated_at"] = _iso_now()
    if action != "keep_or_promote":
        lreg["last_verdict"] = "hold_live"

    sstat["updated_at"] = _iso_now()
    if action != "keep_or_promote":
        sstat["live_enabled"] = False
        sstat["kill_switch_active"] = True

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "canary_feedback_report.json"
    execution_feedback_item = {
        "candidate_id": candidate_id,
        "experiment_id": "",
        "feedback_status": (
            "execution_ok"
            if action == "keep_or_promote"
            else "slippage_bad"
            if action == "tighten_constraints"
            else "fill_quality_bad"
        ),
        "next_action": next_action,
        "market": "",
        "symbol": "",
        "strategy_family": "",
    }
    payload = {
        "cycle_id": cycle_id,
        "candidate_id": candidate_id,
        "feedback_action": action,
        "feedback_status": feedback_status,
        "next_action": next_action,
        "execution_feedback_item": execution_feedback_item,
        "samples": [
            {"case": "canary_ok", "action": "keep_or_promote"},
            {"case": "canary_bad", "action": "demote_or_pause"},
        ],
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "candidate_lifecycle_index.json").write_text(json.dumps(lifecycle, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "active_registry.json").write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "launch_registry.json").write_text(json.dumps(lreg, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "system_status.json").write_text(json.dumps(sstat, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "canary_feedback_report_json": str(path),
        "canary_feedback_report": payload,
        "execution_feedback_item": execution_feedback_item,
    }

