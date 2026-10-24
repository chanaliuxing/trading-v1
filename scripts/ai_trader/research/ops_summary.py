from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


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


def _load_json(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except Exception:
        return {}
    return {}


def persist_launch_registry(
    *,
    cycle_id: str,
    launch_verdict: Mapping[str, Any],
    launch_batch: Mapping[str, Any],
    active_registry: Mapping[str, Any],
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> str:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "launch_registry.json"
    active_rows = [_as_dict(x) for x in _as_list(_as_dict(active_registry).get("items"))]
    by_mode = {"shadow": [], "paper": [], "small_live": []}
    for row in active_rows:
        mode = str(row.get("mode") or "paper")
        cid = str(row.get("candidate_id") or "")
        status = str(row.get("status") or "")
        if not cid:
            continue
        if mode not in by_mode:
            mode = "paper"
        by_mode[mode].append({"candidate_id": cid, "status": status})

    payload = {
        "cycle_id": cycle_id,
        "updated_at": _iso_now(),
        "last_verdict": str(_as_dict(launch_verdict).get("verdict") or "hold_paper"),
        "last_batch_id": str(_as_dict(launch_batch).get("batch_id") or ""),
        "shadow": by_mode["shadow"],
        "paper": by_mode["paper"],
        "small_live": by_mode["small_live"],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def persist_system_status(
    *,
    cycle_id: str,
    operator_controls: Mapping[str, Any],
    risk_guard_report: Mapping[str, Any],
    launch_verdict: Mapping[str, Any],
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> str:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "system_status.json"
    controls = _as_dict(operator_controls)
    risk_summary = _as_dict(_as_dict(risk_guard_report).get("summary"))
    payload = {
        "cycle_id": cycle_id,
        "updated_at": _iso_now(),
        "paper_enabled": bool(controls.get("paper_only", True)) or not bool(controls.get("live_enabled")),
        "live_enabled": bool(controls.get("live_enabled")),
        "global_pause": bool(controls.get("global_pause")),
        "kill_switch_active": int(risk_summary.get("kill_switch_count") or 0) > 0,
        "last_verdict": str(_as_dict(launch_verdict).get("verdict") or "hold_paper"),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def build_ops_summary(
    *,
    cycle_id: str,
    active_registry: Mapping[str, Any],
    auto_halt_report: Mapping[str, Any],
    candidate_lifecycle_report: Mapping[str, Any],
    live_feedback: Mapping[str, Any],
) -> Dict[str, Any]:
    registry_rows = [_as_dict(x) for x in _as_list(_as_dict(active_registry).get("items"))]
    halt_rows = [_as_dict(x) for x in _as_list(_as_dict(auto_halt_report).get("items"))]
    lifecycle_transitions = [_as_dict(x) for x in _as_list(_as_dict(candidate_lifecycle_report).get("transitions"))]
    feedback_rows = [_as_dict(x) for x in _as_list(_as_dict(live_feedback).get("items"))]

    active = sum(1 for x in registry_rows if str(x.get("status") or "") == "active")
    paused_or_halted = sum(1 for x in registry_rows if str(x.get("status") or "") in {"inactive", "halted"})
    halted = sum(1 for x in registry_rows if str(x.get("status") or "") == "halted")
    executed = sum(1 for x in feedback_rows if str(x.get("feedback_status") or "") == "execution_ok")
    blocked = sum(1 for x in feedback_rows if str(x.get("feedback_status") or "") in {"fill_quality_bad", "slippage_bad"})
    demoted = sum(1 for x in halt_rows if "demote" in str(x.get("action") or ""))
    retired = sum(1 for x in lifecycle_transitions if str(x.get("to") or "") == "retired")
    issues = sum(1 for x in feedback_rows if str(x.get("next_action") or "") not in {"", "keep_candidate"})

    next_actions: list[str] = []
    if halted > 0:
        next_actions.append("review_halted_candidates")
    if blocked > 0:
        next_actions.append("tighten_execution_constraints")
    if active == 0:
        next_actions.append("promote_or_retest_candidates")
    if not next_actions:
        next_actions.append("system_operational")

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "active_candidates": active,
        "paused_candidates": paused_or_halted - halted if paused_or_halted >= halted else 0,
        "halted_candidates": halted,
        "today_executed": executed,
        "today_blocked": blocked,
        "today_demoted": demoted,
        "today_retired": retired,
        "today_live_feedback_issues": issues,
        "next_required_actions": next_actions,
    }

