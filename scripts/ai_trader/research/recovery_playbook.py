from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def run_recovery_playbook(
    *,
    cycle_id: str,
    broker_session: Mapping[str, Any],
    account_snapshot: Mapping[str, Any],
    positions_snapshot: Mapping[str, Any],
    orders_state: Mapping[str, Any],
    kill_switch_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    has_session = bool(_as_dict(broker_session).get("session_id"))
    has_account = bool(_as_dict(account_snapshot))
    has_positions = isinstance(_as_dict(positions_snapshot).get("positions"), list)
    has_orders = isinstance(_as_dict(orders_state).get("items"), list)
    kill_active = bool(_as_dict(kill_switch_report).get("kill_switch_active"))

    steps = [
        {"step": "load_broker_session", "ok": has_session},
        {"step": "load_account_snapshot", "ok": has_account},
        {"step": "load_positions_snapshot", "ok": has_positions},
        {"step": "load_orders_state", "ok": has_orders},
        {"step": "apply_kill_switch_state", "ok": True, "kill_switch_active": kill_active},
        {"step": "rebuild_runtime_views", "ok": has_session and has_account and has_positions and has_orders},
    ]
    recovered = all(bool(_as_dict(x).get("ok")) for x in steps)
    payload = {
        "cycle_id": cycle_id,
        "recovered": recovered,
        "status": "success" if recovered else "failed",
        "steps": steps,
    }
    path = out / "recovery_report.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"recovery_report_json": str(path), "recovery_report": payload}

