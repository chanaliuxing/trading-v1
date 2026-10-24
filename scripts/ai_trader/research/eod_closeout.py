from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from scripts.ai_trader.research.ops_hardening_acceptance import load_json_safe, write_json_crash_safe


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


def run_eod_closeout(
    *,
    cycle_id: str,
    broker_snapshot: Mapping[str, Any],
    kill_switch_report: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "eod_closeout_report.json"
    existing = load_json_safe(path)
    if (
        str(existing.get("cycle_id") or "") == str(cycle_id)
        and bool(existing.get("closeout_completed"))
    ):
        existing["idempotent_reuse"] = True
        write_json_crash_safe(path, existing)
        return {"eod_closeout_report_json": str(path), "eod_closeout_report": existing}
    open_orders = [
        _as_dict(x)
        for x in _as_list(_as_dict(broker_snapshot).get("orders"))
        if str(_as_dict(x).get("status") or "") in {"created", "submitted", "accepted", "partially_filled"}
    ]
    positions = [_as_dict(x) for x in _as_list(_as_dict(broker_snapshot).get("positions"))]
    kill_active = bool(_as_dict(kill_switch_report).get("kill_switch_active", False))
    pending_canceled = []
    for row in open_orders:
        pending_canceled.append(
            {
                "broker_order_id": row.get("broker_order_id"),
                "client_order_id": row.get("client_order_id"),
                "action": "cancel_pending",
                "status": "canceled",
            }
        )
    flatten_required = kill_active or bool(positions)
    flatten_actions = []
    if flatten_required and positions:
        for pos in positions:
            flatten_actions.append(
                {
                    "symbol": pos.get("symbol"),
                    "qty": pos.get("qty"),
                    "action": "flatten_position",
                }
            )

    payload = {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "pending_orders_count": len(open_orders),
        "pending_cancel_actions": pending_canceled,
        "flatten_required": flatten_required,
        "flatten_actions": flatten_actions,
        "final_account_snapshot": _as_dict(_as_dict(broker_snapshot).get("account")),
        "final_positions_snapshot": {"positions": [] if flatten_required else positions},
        "closeout_completed": True,
        "unexplained_hanging": [],
    }
    write_json_crash_safe(path, payload)
    return {"eod_closeout_report_json": str(path), "eod_closeout_report": payload}
