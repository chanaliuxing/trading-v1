from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional


def _day_dir(base: Optional[str] = None) -> Path:
    runs = Path(base or "runs/llm_trader") / date.today().isoformat()
    runs.mkdir(parents=True, exist_ok=True)
    return runs


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return None


def save_json(path: Path, obj: Dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False


def plan_path(base: Optional[str] = None) -> Path:
    return _day_dir(base) / "daily_plan.json"


def pending_path(base: Optional[str] = None) -> Path:
    return _day_dir(base) / "pending_orders.json"


def summary_path(base: Optional[str] = None) -> Path:
    return _day_dir(base) / "state_summary.json"


def load_plan(base: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return load_json(plan_path(base))


def save_plan(plan: Dict[str, Any], base: Optional[str] = None) -> bool:
    return save_json(plan_path(base), plan)


def load_summary(base: Optional[str] = None) -> Dict[str, Any]:
    return load_json(summary_path(base)) or {}


def save_summary(s: Dict[str, Any], base: Optional[str] = None) -> bool:
    return save_json(summary_path(base), s)

