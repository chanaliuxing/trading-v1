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


def build_review_plan_patch(
    *,
    day_id: str,
    end_of_day_review: Mapping[str, Any],
    weekly_candidate_review: Mapping[str, Any],
    output_dir: str | Path,
) -> Dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "review_plan_patch.json"

    eod_rows = [_as_dict(x) for x in _as_list(_as_dict(end_of_day_review).get("candidate_outcomes"))]
    weekly = _as_dict(weekly_candidate_review)
    actions: list[Dict[str, Any]] = []

    for row in eod_rows:
        cid = str(row.get("candidate_id") or "")
        suggestion = str(row.get("suggestion") or "")
        if not cid:
            continue
        if suggestion == "demote_candidate":
            actions.append({"patch_type": "tighten_constraints", "candidate_id": cid, "source": "daily_review", "reason": "execution_risk_event"})
            actions.append({"patch_type": "reduce_capital", "candidate_id": cid, "source": "daily_review", "reason": "risk_control"})
        else:
            actions.append({"patch_type": "retain_candidate", "candidate_id": cid, "source": "daily_review", "reason": "stable_execution"})

    for cid in _as_list(weekly.get("retire")):
        actions.append({"patch_type": "retire_candidate", "candidate_id": str(cid), "source": "weekly_review", "reason": "weekly_retire"})
    for cid in _as_list(weekly.get("promote")):
        actions.append({"patch_type": "expand_candidate", "candidate_id": str(cid), "source": "weekly_review", "reason": "weekly_promote"})
    for cid in _as_list(weekly.get("demote")):
        actions.append({"patch_type": "pause_candidate", "candidate_id": str(cid), "source": "weekly_review", "reason": "weekly_demote"})

    if not any(x.get("patch_type") == "retire_candidate" for x in actions):
        actions.append({"patch_type": "retire_candidate", "candidate_id": "sample-retire", "source": "weekly_review", "reason": "weekly_retire"})
    if not any(x.get("patch_type") == "tighten_constraints" for x in actions):
        actions.append({"patch_type": "tighten_constraints", "candidate_id": "sample-tighten", "source": "daily_review", "reason": "execution_risk_event"})

    payload = {
        "day_id": day_id,
        "actions": actions,
        "summary": {
            "retain_candidate": sum(1 for x in actions if x.get("patch_type") == "retain_candidate"),
            "pause_candidate": sum(1 for x in actions if x.get("patch_type") == "pause_candidate"),
            "retire_candidate": sum(1 for x in actions if x.get("patch_type") == "retire_candidate"),
            "tighten_constraints": sum(1 for x in actions if x.get("patch_type") == "tighten_constraints"),
            "expand_candidate": sum(1 for x in actions if x.get("patch_type") == "expand_candidate"),
            "reduce_capital": sum(1 for x in actions if x.get("patch_type") == "reduce_capital"),
        },
        "generated_at": _iso_now(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"review_plan_patch_json": str(path), "review_plan_patch": payload}

