from __future__ import annotations

import json
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


def build_live_candidates(
    *,
    paper_candidates_dir: str | Path,
    output_dir: str | Path,
    default_broker_profile: str = "paper",
    operator_state_root: str | Path = "runs/ai_trader/research/state",
) -> Dict[str, Any]:
    src = Path(paper_candidates_dir)
    out = Path(output_dir) / "live_candidates"
    out.mkdir(parents=True, exist_ok=True)
    controls = load_operator_controls(state_root=operator_state_root, create_if_missing=True)
    blocked_candidates = {str(x) for x in _as_list(controls.get("blocked_candidates"))}
    blocked_symbols = {str(x).upper() for x in _as_list(controls.get("blocked_symbols"))}

    manifests: list[str] = []
    if not src.exists():
        return {"live_candidates_dir": str(out), "live_manifests": manifests}

    for candidate_dir in sorted(src.iterdir()):
        if not candidate_dir.is_dir():
            continue
        paper_manifest_path = candidate_dir / "paper_manifest.json"
        if not paper_manifest_path.exists():
            continue
        paper_manifest = _as_dict(json.loads(paper_manifest_path.read_text(encoding="utf-8")))
        candidate_id = str(paper_manifest.get("candidate_id") or candidate_dir.name)
        experiment_id = str(paper_manifest.get("experiment_id") or candidate_id)
        allocation = _as_dict(paper_manifest.get("allocation"))
        constraints = _as_dict(paper_manifest.get("execution_constraints"))
        symbols = _as_list(_as_dict(_as_dict(paper_manifest.get("entrypoint")).get("universe")).get("symbols"))
        if not symbols:
            symbols = _as_list(_as_dict(_as_dict(paper_manifest.get("entrypoint")).get("universe")).get("underlying"))
        if not symbols:
            symbols = _as_list(paper_manifest.get("symbols"))

        live_dir = out / candidate_id
        live_dir.mkdir(parents=True, exist_ok=True)
        live_manifest = {
            "candidate_id": candidate_id,
            "experiment_id": experiment_id,
            "target_weight": float(allocation.get("target_weight") or 0.0),
            "max_capital": float(allocation.get("max_capital") or 0.0),
            "symbols": [str(s).upper() for s in symbols if str(s).strip()],
            "order_constraints": {
                "max_orders_per_day": int(constraints.get("max_orders_per_day") or 20),
                "max_notional": float(constraints.get("max_notional") or allocation.get("max_capital") or 0.0),
                "limit_only": bool(_as_dict(constraints).get("use_limit_order_only", False)),
            },
            "risk_limits": {
                "risk_budget_bps": float(allocation.get("risk_budget_bps") or constraints.get("risk_budget_bps") or 0.0),
                "daily_max_drawdown_bps": 80.0,
                "max_leverage": 1.0,
            },
            "broker_profile": "paper" if bool(controls.get("paper_only", True)) else default_broker_profile,
            "operator_controls_applied": {
                "global_pause": bool(controls.get("global_pause")),
                "live_enabled": bool(controls.get("live_enabled")),
                "blocked_candidate": candidate_id in blocked_candidates,
                "blocked_symbol_hit": any(sym in blocked_symbols for sym in [str(s).upper() for s in symbols]),
            },
            "source": {"paper_manifest": str(paper_manifest_path)},
        }
        manifest_path = live_dir / "live_manifest.json"
        manifest_path.write_text(json.dumps(live_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        manifests.append(str(manifest_path))
    return {"live_candidates_dir": str(out), "live_manifests": manifests}
