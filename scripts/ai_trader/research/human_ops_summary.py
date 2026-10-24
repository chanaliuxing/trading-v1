from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.policy_compatibility_observability import (
    DEFAULT_POLICY_COMPATIBILITY_SUMMARY_PATH,
    DEFAULT_POLICY_OBSERVABILITY_CONSUMERS_PATH,
    build_policy_compatibility_observability,
)


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


def build_human_ops_summary(
    *,
    day_id: str,
    trading_day_status: Mapping[str, Any],
    ops_control_center: Mapping[str, Any],
    end_of_day_review: Mapping[str, Any],
    weekly_candidate_review: Mapping[str, Any],
    review_plan_patch: Mapping[str, Any],
    output_dir: str | Path,
    policy_compatibility_summary_path: str | Path = DEFAULT_POLICY_COMPATIBILITY_SUMMARY_PATH,
    policy_observability_consumers_path: str | Path = DEFAULT_POLICY_OBSERVABILITY_CONSUMERS_PATH,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "human_ops_summary.json"

    ops = _as_dict(ops_control_center)
    eod = _as_dict(end_of_day_review)
    weekly = _as_dict(weekly_candidate_review)
    patch = _as_dict(review_plan_patch)
    outcomes = [_as_dict(x) for x in _as_list(eod.get("candidate_outcomes"))]

    made_money = [str(x.get("candidate_id") or "") for x in outcomes if str(x.get("suggestion") or "") == "retain_candidate"][:8]
    lost_money = [str(x.get("candidate_id") or "") for x in outcomes if str(x.get("suggestion") or "") == "demote_candidate"][:8]
    changed = [_as_dict(x) for x in _as_list(patch.get("actions")) if str(_as_dict(x).get("patch_type") or "") in {"retire_candidate", "tighten_constraints", "expand_candidate", "reduce_capital"}][:12]
    policy_compatibility_view = build_policy_compatibility_observability(
        policy_compatibility_summary_path,
        consumers_inventory_path=policy_observability_consumers_path,
    )

    payload = {
        "day_id": day_id,
        "today_status": {
            "stage": str(_as_dict(trading_day_status).get("current_stage") or "unknown"),
            "live_enabled": bool(ops.get("live_enabled", False)),
            "system_mode": str(ops.get("system_mode") or "paper"),
        },
        "what_ran": {
            "batches": list(_as_list(ops.get("active_batches"))),
            "candidates": list(_as_list(ops.get("active_candidates"))),
            "orders": int(_as_dict(eod.get("orders_summary")).get("total_orders") or 0),
            "fills": int(_as_dict(eod.get("fills_summary")).get("total_fills") or 0),
        },
        "what_blocked": list(_as_list(ops.get("warnings"))),
        "what_halted": list(_as_list(ops.get("halts"))),
        "what_made_money": made_money,
        "what_lost_money": lost_money,
        "what_changed_for_tomorrow": changed,
        "policy_compatibility_observability": policy_compatibility_view,
        "weekly_snapshot": {
            "promote": _as_list(weekly.get("promote"))[:6],
            "demote": _as_list(weekly.get("demote"))[:6],
            "retire": _as_list(weekly.get("retire"))[:6],
            "watchlist": _as_list(weekly.get("watchlist"))[:6],
        },
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"human_ops_summary_json": str(path), "human_ops_summary": payload}
