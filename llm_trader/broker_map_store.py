from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List


def _file(base: str | None = None) -> Path:
    d = Path(base or "runs/llm_trader") / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d / "broker_orders.json"


def load_map(base: str | None = None) -> List[Dict[str, Any]]:
    p = _file(base)
    if not p.exists():
        return []
    try:
        obj = json.loads(p.read_text("utf-8"))
        return obj.get("orders", []) if isinstance(obj, dict) else []
    except Exception:
        return []


def save_map(orders: List[Dict[str, Any]], base: str | None = None) -> bool:
    p = _file(base)
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"orders": orders}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:
        return False


def exists_pending(pending_id: str, base: str | None = None) -> bool:
    for o in load_map(base):
        if str(o.get("pending_id")) == str(pending_id):
            return True
    return False


def append_mapping(entry: Dict[str, Any], base: str | None = None) -> bool:
    m = load_map(base)
    m.append(entry)
    return save_map(m, base)

