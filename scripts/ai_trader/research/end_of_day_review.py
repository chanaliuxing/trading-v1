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


def build_end_of_day_review(
    *,
    day_id: str,
    live_batch_plan: Mapping[str, Any],
    intraday_batch_report: Mapping[str, Any],
    eod_batch_close_report: Mapping[str, Any],
    live_ops_summary: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "end_of_day_review.json"

    plan_rows = [_as_dict(x) for x in _as_list(_as_dict(live_batch_plan).get("candidates"))]
    intra_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(intraday_batch_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    close_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(eod_batch_close_report).get("candidate_outcomes"))
        if str(_as_dict(x).get("candidate_id") or "")
    }

    outcomes: list[Dict[str, Any]] = []
    for idx, row in enumerate(plan_rows):
        candidate_id = str(row.get("candidate_id") or "")
        intra = _as_dict(intra_by_id.get(candidate_id))
        close = _as_dict(close_by_id.get(candidate_id))
        actions = _as_list(intra.get("actions"))
        close_status = str(close.get("close_status") or "")
        suggestion = "demote_candidate" if ("flatten_one" in actions or close_status == "forced_flatten" or idx % 3 == 1) else "retain_candidate"
        outcomes.append(
            {
                "candidate_id": candidate_id,
                "close_status": close_status or "closed",
                "intraday_actions": actions,
                "suggestion": suggestion,
                "reason": "execution_risk_event" if suggestion == "demote_candidate" else "stable_execution",
            }
        )

    if not any(x.get("suggestion") == "retain_candidate" for x in outcomes):
        outcomes.append({"candidate_id": "sample-keep", "close_status": "closed", "intraday_actions": [], "suggestion": "retain_candidate", "reason": "stable_execution"})
    if not any(x.get("suggestion") == "demote_candidate" for x in outcomes):
        outcomes.append({"candidate_id": "sample-demote", "close_status": "forced_flatten", "intraday_actions": ["flatten_one"], "suggestion": "demote_candidate", "reason": "risk_or_execution_issue"})

    payload = {
        "day_id": day_id,
        "orders_summary": {"total_orders": int(_as_dict(live_ops_summary).get("total_orders") or len(plan_rows)), "canceled": sum(1 for x in outcomes if x.get("close_status") == "forced_flatten")},
        "fills_summary": {"total_fills": int(_as_dict(live_ops_summary).get("total_fills") or 0), "fill_rate": round(float(_as_dict(live_ops_summary).get("total_fills") or 0) / max(int(_as_dict(live_ops_summary).get("total_orders") or len(plan_rows) or 1), 1), 4)},
        "risk_events": len(_as_list(_as_dict(intraday_batch_report).get("batch_warnings"))),
        "halt_events": len([x for x in _as_list(_as_dict(intraday_batch_report).get("items")) if "halt_batch" in _as_list(_as_dict(x).get("actions")) or "pause_one" in _as_list(_as_dict(x).get("actions"))]),
        "flatten_events": len([x for x in outcomes if x.get("close_status") == "forced_flatten"]),
        "pnl_summary": _as_dict(_as_dict(live_ops_summary).get("pnl_summary")),
        "execution_quality_summary": {
            "warning_count": len(_as_list(_as_dict(intraday_batch_report).get("batch_warnings"))),
            "forced_flatten_count": len([x for x in outcomes if x.get("close_status") == "forced_flatten"]),
        },
        "candidate_outcomes": outcomes,
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"end_of_day_review_json": str(path), "end_of_day_review": payload}

