from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.operator_controls import load_operator_controls


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


def evaluate_live_risk_guard(
    *,
    cycle_id: str,
    live_candidates_dir: str | Path,
    single_trade_max_risk_bps: float = 25.0,
    daily_max_drawdown_bps: float = 80.0,
    max_position_notional: float = 75_000.0,
    blacklist_symbols: list[str] | None = None,
    cooldown_candidates: list[str] | None = None,
    simulate_kill_switch_candidate: str | None = None,
    operator_state_root: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, Any]:
    src = Path(live_candidates_dir)
    controls = load_operator_controls(state_root=operator_state_root, create_if_missing=True)
    blacklist = {str(x).upper() for x in (blacklist_symbols or [])} | {
        str(x).upper() for x in _as_list(controls.get("blocked_symbols"))
    }
    cooldown = {str(x) for x in (cooldown_candidates or [])} | {str(x) for x in _as_list(controls.get("cooldown_candidates"))}
    blocked_candidates = {str(x) for x in _as_list(controls.get("blocked_candidates"))}
    global_pause = bool(controls.get("global_pause"))
    items: list[Dict[str, Any]] = []
    kill_switch_count = 0
    for candidate_dir in sorted(src.iterdir()) if src.exists() else []:
        if not candidate_dir.is_dir():
            continue
        manifest_path = candidate_dir / "live_manifest.json"
        if not manifest_path.exists():
            continue
        m = _as_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        cid = str(m.get("candidate_id") or candidate_dir.name)
        exp_id = str(m.get("experiment_id") or "")
        symbols = [str(x).upper() for x in _as_list(m.get("symbols"))]
        limits = _as_dict(m.get("risk_limits"))
        order_constraints = _as_dict(m.get("order_constraints"))
        risk_bps = float(limits.get("risk_budget_bps") or 0.0)
        max_notional = float(m.get("max_capital") or order_constraints.get("max_notional") or 0.0)
        dd_limit = float(limits.get("daily_max_drawdown_bps") or daily_max_drawdown_bps)
        reasons: list[str] = []
        allowed = True
        kill_switch = False

        if cid in cooldown:
            allowed = False
            reasons.append("cooldown_window")
        if global_pause:
            allowed = False
            reasons.append("global_pause")
        if cid in blocked_candidates:
            allowed = False
            reasons.append("blocked_candidate")
        if any(sym in blacklist for sym in symbols):
            allowed = False
            reasons.append("symbol_blacklisted")
        if risk_bps > single_trade_max_risk_bps:
            allowed = False
            reasons.append("single_trade_risk_exceeded")
        if max_notional > max_position_notional:
            allowed = False
            reasons.append("position_notional_exceeded")
        if dd_limit <= 0:
            allowed = False
            reasons.append("invalid_daily_drawdown_limit")
        if simulate_kill_switch_candidate and cid == simulate_kill_switch_candidate:
            allowed = False
            kill_switch = True
            reasons.append("kill_switch_simulated")
        # deterministic sample trigger: overly concentrated allocation
        if float(m.get("target_weight") or 0.0) > 0.65:
            allowed = False
            kill_switch = True
            reasons.append("kill_switch_concentration")

        if kill_switch:
            kill_switch_count += 1
        items.append(
            {
                "candidate_id": cid,
                "experiment_id": exp_id,
                "allowed": allowed,
                "kill_switch": kill_switch,
                "risk_checks": {
                    "single_trade_max_risk_bps": single_trade_max_risk_bps,
                    "daily_max_drawdown_bps": dd_limit,
                    "max_position_notional": max_position_notional,
                },
                "reasons": reasons or ["passed"],
                "evaluated_at": _iso_now(),
            }
        )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": items,
        "summary": {
            "candidate_count": len(items),
            "allowed_count": sum(1 for x in items if x.get("allowed")),
            "blocked_count": sum(1 for x in items if not x.get("allowed")),
            "kill_switch_count": kill_switch_count,
        },
    }
