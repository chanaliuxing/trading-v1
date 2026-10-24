from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def sync_account_snapshot(
    *,
    cycle_id: str,
    broker_snapshot: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    account = _as_dict(_as_dict(broker_snapshot).get("account"))
    payload = {
        "cycle_id": cycle_id,
        "cash": float(account.get("cash") or 0.0),
        "buying_power": float(account.get("buying_power") or 0.0),
        "equity": float(account.get("equity") or 0.0),
        "realized_pnl": float(account.get("realized_pnl") or 0.0),
        "unrealized_pnl": float(account.get("unrealized_pnl") or 0.0),
        "day_pnl": float(account.get("day_pnl") or 0.0),
    }
    path = out / "account_snapshot.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"account_snapshot_json": str(path), "account_snapshot": payload}

