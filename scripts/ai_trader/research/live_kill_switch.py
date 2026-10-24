from __future__ import annotations

import json
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


def run_live_kill_switch(
    *,
    cycle_id: str,
    operator_controls: Mapping[str, Any],
    live_risk_report: Mapping[str, Any],
    positions_snapshot: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    controls = _as_dict(operator_controls)
    risks = _as_dict(live_risk_report)
    positions = [_as_dict(x) for x in _as_list(_as_dict(positions_snapshot).get("positions"))]
    triggered = []
    actions = []

    if bool(controls.get("global_pause")):
        triggered.append("global_kill_switch")
        actions.append({"action": "pause_live_globally"})
    blocked_candidates = [str(x) for x in _as_list(controls.get("blocked_candidates"))]
    for cid in blocked_candidates:
        actions.append({"action": "candidate_kill_switch", "candidate_id": cid})
    blocked_symbols = [str(x).upper() for x in _as_list(controls.get("blocked_symbols"))]
    for sym in blocked_symbols:
        actions.append({"action": "symbol_kill_switch", "symbol": sym})
    if _as_list(risks.get("flatten_actions")):
        triggered.append("risk_violation_kill_switch")
        actions.extend(_as_list(risks.get("flatten_actions")))
    if actions and positions:
        actions.append({"action": "flatten_all", "position_count": len(positions)})

    payload = {
        "cycle_id": cycle_id,
        "triggered": sorted(set(triggered)),
        "actions": actions,
        "kill_switch_active": bool(actions),
    }
    path = out / "kill_switch_report.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"kill_switch_report_json": str(path), "kill_switch_report": payload}

