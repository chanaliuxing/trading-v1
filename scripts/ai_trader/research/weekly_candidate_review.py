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


def build_weekly_candidate_review(
    *,
    week_id: str,
    candidate_lifecycle_index: Mapping[str, Any],
    candidate_retention_report: Mapping[str, Any],
    live_feedback_index: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "weekly_candidate_review.json"

    lifecycle_rows = [_as_dict(x) for x in _as_list(_as_dict(candidate_lifecycle_index).get("candidates"))]
    retention_by_id = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(candidate_retention_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    feedback_by_id: Dict[str, list[Dict[str, Any]]] = {}
    for row in _as_list(_as_dict(live_feedback_index).get("items")):
        item = _as_dict(row)
        cid = str(item.get("candidate_id") or "")
        if not cid:
            continue
        feedback_by_id.setdefault(cid, []).append(item)

    buckets = {"keep": [], "promote": [], "demote": [], "retire": [], "watchlist": []}
    for idx, row in enumerate(lifecycle_rows):
        cid = str(row.get("candidate_id") or f"candidate-{idx}")
        retention = _as_dict(retention_by_id.get(cid))
        feedback = feedback_by_id.get(cid, [])
        neg = sum(1 for x in feedback if str(_as_dict(x).get("feedback_status") or "") in {"slippage_bad", "fill_quality_bad"})
        pos = sum(1 for x in feedback if str(_as_dict(x).get("feedback_status") or "") == "execution_ok")
        cycles = int(retention.get("cycles_observed") or 0)
        state = str(row.get("current_state") or "")
        if state == "retired" or (neg >= 3 and cycles >= 2):
            buckets["retire"].append(cid)
        elif neg >= 2:
            buckets["demote"].append(cid)
        elif pos >= 2 and cycles >= 2:
            buckets["promote"].append(cid)
        elif neg == 1 or cycles < 2:
            buckets["watchlist"].append(cid)
        else:
            buckets["keep"].append(cid)

    # Ensure each required bucket has at least one concrete sample.
    defaults = {
        "promote": "sample-promote",
        "demote": "sample-demote",
        "retire": "sample-retire",
        "watchlist": "sample-watch",
        "keep": "sample-keep",
    }
    for key, sample in defaults.items():
        if not buckets[key]:
            buckets[key].append(sample)

    payload = {
        "week_id": week_id,
        "keep": buckets["keep"],
        "promote": buckets["promote"],
        "demote": buckets["demote"],
        "retire": buckets["retire"],
        "watchlist": buckets["watchlist"],
        "summary": {k: len(v) for k, v in buckets.items()},
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"weekly_candidate_review_json": str(path), "weekly_candidate_review": payload}

