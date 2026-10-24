from __future__ import annotations
import yaml
from pathlib import Path
from typing import Dict, Any, List


def load_promotion_policy(path: str = "scripts/ai_trader/config/promotion_policy.yml") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"criteria": {"sharpe_min": 1.0, "dd_max": 0.20, "improvement_pct": 10}}
    try:
        return yaml.safe_load(p.read_text("utf-8")) or {}
    except Exception:
        return {}


def build_pending_actions(dxp: Dict[str, Any], rl_eval: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    pending: List[Dict[str, Any]] = []
    # Example: apply prescription actions with policy gate
    criteria = (policy.get("promotion_policy") or {}).get("criteria") or policy.get("criteria") or {}
    sharpe_min = float(criteria.get("sharpe_min", 1.0))
    dd_max = float(criteria.get("dd_max", 0.20))

    rl_ok = bool((rl_eval or {}).get("gate", {}).get("ok", True))
    if not rl_ok:
        pending.append({"action": "disable_rl", "reason": "rl_gate_failed"})

    for step in (dxp.get("prescription") or []):
        action = step.get("action")
        params = step.get("params") or {}
        # Gate example: only allow sizing increases when consistency>=0.65
        if action == "reduce_sizing_multiplier" or action == "increase_conf_floor":
            if float(dxp.get("facts", {}).get("consistency", 0.0)) < 0.65:
                pending.append({"action": action, "params": params, "reason": "consistency_low"})
    return {"pending": pending}


def write_pending_actions(out_dir: str, payload: Dict[str, Any]) -> str:
    p = Path(out_dir) / "pending_actions.yml"
    p.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return str(p)

