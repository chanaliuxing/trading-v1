from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_broker_readonly_probe(
    *,
    cycle_id: str,
    broker: Any,
    broker_profile: str,
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "broker_readonly_probe.json"
    if str(broker_profile) != "real":
        payload = {
            "cycle_id": cycle_id,
            "broker_profile": broker_profile,
            "status": "skipped",
            "reason": "profile_not_real",
            "generated_at": _iso_now(),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"broker_readonly_probe_json": str(path), "broker_readonly_probe": payload}

    try:
        connect = dict(getattr(broker, "connect")() or {})
        account = dict(getattr(broker, "get_account")() or {})
        positions = list(getattr(broker, "get_positions")() or [])
        open_orders = list(getattr(broker, "list_open_orders")() or [])
        recent_fills = list(getattr(broker, "export_state")().get("fills", []) or [])
        status = "success" if bool(connect.get("ok", False)) else "controlled_fail"
        payload = {
            "cycle_id": cycle_id,
            "broker_profile": broker_profile,
            "status": status,
            "connect": connect,
            "account_probe": {"ok": bool(account), "account": account},
            "positions_probe": {"ok": True, "count": len(positions), "positions": positions},
            "open_orders_probe": {"ok": True, "count": len(open_orders), "open_orders": open_orders},
            "recent_fills_probe": {"ok": True, "count": len(recent_fills), "fills": recent_fills[:20]},
            "generated_at": _iso_now(),
        }
    except Exception as exc:
        payload = {
            "cycle_id": cycle_id,
            "broker_profile": broker_profile,
            "status": "controlled_fail",
            "reason": f"probe_exception:{exc}",
            "generated_at": _iso_now(),
        }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"broker_readonly_probe_json": str(path), "broker_readonly_probe": payload}

