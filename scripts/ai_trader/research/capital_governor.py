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


def evaluate_capital_governor(
    *,
    cycle_id: str,
    live_candidates_dir: str | Path,
    operator_controls: Mapping[str, Any],
    live_feedback_index: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    controls = _as_dict(operator_controls)
    max_live_capital = float(controls.get("max_live_capital") or 0.0)
    market_caps = {"stocks": max_live_capital * 0.7, "options": max_live_capital * 0.4}
    per_candidate_cap = max_live_capital * 0.35
    daily_new_risk_cap = max_live_capital * 0.5
    feedback_rows = _as_list(_as_dict(live_feedback_index).get("items"))
    losses_by_candidate: Dict[str, int] = {}
    for row in feedback_rows:
        item = _as_dict(row)
        cid = str(item.get("candidate_id") or "")
        if not cid:
            continue
        realized = float(item.get("realized_pnl") or 0.0)
        if realized < 0:
            losses_by_candidate[cid] = losses_by_candidate.get(cid, 0) + 1

    src = Path(live_candidates_dir)
    items: list[Dict[str, Any]] = []
    market_used = {"stocks": 0.0, "options": 0.0}
    total_used = 0.0
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
        market = "options" if any(sym.endswith("P") or sym.endswith("C") for sym in symbols) else "stocks"
        requested_cap = float(m.get("max_capital") or 0.0)
        requested_risk = float(_as_dict(m.get("risk_limits")).get("risk_budget_bps") or 0.0)
        reasons: list[str] = []
        decision = "approved"
        effective_cap = requested_cap
        effective_risk = requested_risk

        if requested_cap > per_candidate_cap:
            effective_cap = per_candidate_cap
            effective_risk = max(1.0, requested_risk * (effective_cap / max(requested_cap, 1.0)))
            reasons.append("per_candidate_cap_downscaled")
            decision = "downscaled"
        if losses_by_candidate.get(cid, 0) >= 2:
            effective_cap = round(effective_cap * 0.5, 2)
            effective_risk = round(effective_risk * 0.6, 2)
            reasons.append("consecutive_losses_downscaled")
            decision = "downscaled"
        if market_used[market] + effective_cap > market_caps[market]:
            decision = "rejected"
            reasons.append("market_cap_exhausted")
            effective_cap = 0.0
            effective_risk = 0.0
        if total_used + effective_cap > max_live_capital:
            decision = "rejected"
            reasons.append("global_cap_exhausted")
            effective_cap = 0.0
            effective_risk = 0.0
        if (total_used + effective_cap) > daily_new_risk_cap and decision != "rejected":
            decision = "rejected"
            reasons.append("daily_new_risk_cap_exhausted")
            effective_cap = 0.0
            effective_risk = 0.0

        market_used[market] += effective_cap
        total_used += effective_cap
        items.append(
            {
                "candidate_id": cid,
                "experiment_id": exp_id,
                "market": market,
                "requested_capital": requested_cap,
                "effective_capital": round(effective_cap, 2),
                "requested_risk_bps": requested_risk,
                "effective_risk_bps": round(effective_risk, 2),
                "decision": decision,
                "reasons": reasons or ["approved"],
                "evaluated_at": _iso_now(),
            }
        )
    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "limits": {
            "max_live_capital": max_live_capital,
            "per_market_capital": market_caps,
            "per_candidate_capital": per_candidate_cap,
            "daily_new_risk_capital": daily_new_risk_cap,
        },
        "items": items,
        "summary": {
            "approved": sum(1 for x in items if x.get("decision") == "approved"),
            "downscaled": sum(1 for x in items if x.get("decision") == "downscaled"),
            "rejected": sum(1 for x in items if x.get("decision") == "rejected"),
        },
    }
