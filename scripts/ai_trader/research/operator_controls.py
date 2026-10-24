from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping


DEFAULT_STATE_ROOT = "runs/ai_trader/research/state"


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def default_operator_controls() -> Dict[str, Any]:
    return {
        "global_pause": False,
        "paper_only": True,
        "live_enabled": False,
        "max_live_capital": 100_000.0,
        "blocked_candidates": [],
        "blocked_symbols": [],
        "force_flatten": False,
        "no_trade_windows": ["12:00-12:15"],
        "cooldown_candidates": [],
        "notes": "default_generated",
    }


def load_operator_controls(
    *,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    create_if_missing: bool = True,
) -> Dict[str, Any]:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "operator_controls.json"
    if not path.exists():
        payload = default_operator_controls()
        if create_if_missing:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        payload = default_operator_controls()
        payload.update(_as_dict(obj))
        return payload
    except Exception:
        payload = default_operator_controls()
        if create_if_missing:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload


def persist_operator_controls(
    controls: Mapping[str, Any],
    *,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> str:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "operator_controls.json"
    payload = default_operator_controls()
    payload.update(_as_dict(controls))
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
