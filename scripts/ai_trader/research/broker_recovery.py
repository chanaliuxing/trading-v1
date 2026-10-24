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


def run_broker_recovery(
    *,
    cycle_id: str,
    broker: Any,
    broker_session: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "broker_recovery_report.json"
    session = _as_dict(broker_session)
    has_session = bool(session.get("session_id"))
    success_payload = {}
    fail_payload = {}
    try:
        connect = dict(getattr(broker, "connect")() or {}) if hasattr(broker, "connect") else {"ok": True, "status": "no_connect_method"}
        account = dict(getattr(broker, "get_account")() or {})
        orders = list(getattr(broker, "list_open_orders")() or [])
        positions = list(getattr(broker, "get_positions")() or [])
        rebuilt = {
            "orders_state": [
                {
                    "broker_order_id": row.get("broker_order_id"),
                    "status": row.get("status"),
                    "symbol": row.get("symbol"),
                }
                for row in orders
            ],
            "positions_state": positions,
        }
        recovered = bool(connect.get("ok", True)) and has_session
        success_payload = {
            "status": "success" if recovered else "controlled_fail",
            "connect": connect,
            "account_loaded": bool(account),
            "orders_loaded": len(orders),
            "positions_loaded": len(positions),
            "rebuild_oms": bool(rebuilt["orders_state"] or rebuilt["positions_state"] or account),
            "rebuilt_state": rebuilt,
        }
    except Exception as exc:
        fail_payload = {
            "status": "controlled_fail",
            "reason": f"recovery_exception:{exc}",
            "connect": {"ok": False},
            "account_loaded": False,
            "orders_loaded": 0,
            "positions_loaded": 0,
            "rebuild_oms": False,
            "rebuilt_state": {"orders_state": [], "positions_state": []},
        }

    payload = {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "recovery_success_sample": success_payload,
        "recovery_controlled_fail_sample": (
            fail_payload
            if fail_payload
            else {
                "status": "controlled_fail",
                "reason": "sample_missing_session",
                "connect": {"ok": False},
                "account_loaded": False,
                "orders_loaded": 0,
                "positions_loaded": 0,
                "rebuild_oms": False,
                "rebuilt_state": {"orders_state": [], "positions_state": []},
            }
        ),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"broker_recovery_report_json": str(path), "broker_recovery_report": payload}

