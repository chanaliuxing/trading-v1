from __future__ import annotations

from datetime import datetime, timezone
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


def build_candidate_retention(
    *,
    cycle_id: str,
    execution_feedback: Mapping[str, Any],
    lifecycle_index: Mapping[str, Any],
) -> Dict[str, Any]:
    lifecycle_rows = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(lifecycle_index).get("candidates"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    feedback_rows = [_as_dict(x) for x in _as_list(_as_dict(execution_feedback).get("items"))]
    feedback_by_candidate = {
        str(x.get("candidate_id") or x.get("experiment_id") or ""): x
        for x in feedback_rows
        if str(x.get("candidate_id") or x.get("experiment_id") or "")
    }

    report_rows: list[Dict[str, Any]] = []
    for candidate_id in sorted(set(list(lifecycle_rows.keys()) + list(feedback_by_candidate.keys()))):
        row = _as_dict(lifecycle_rows.get(candidate_id))
        prev = _as_dict(row.get("retention"))
        fb = _as_dict(feedback_by_candidate.get(candidate_id))
        status = str(fb.get("feedback_status") or "")
        old_cycles = int(prev.get("cycles_observed") or 0)
        old_shadow_runs = int(prev.get("shadow_runs") or 0)
        old_paper_runs = int(prev.get("paper_runs") or 0)
        old_avg_slippage = float(prev.get("avg_slippage_delta") or 0.0)
        old_avg_fill = float(prev.get("avg_fill_quality") or 0.0)
        old_streak = int(prev.get("promotion_streak") or 0)
        old_demotion_count = int(prev.get("demotion_count") or 0)

        if fb:
            cycles_observed = old_cycles + 1
            shadow_runs = old_shadow_runs + 1
            paper_runs = old_paper_runs + (1 if str(fb.get("run_mode") or "") == "paper" else 0)
            slippage = float(fb.get("slippage_delta") or 0.0)
            fill_quality = float(fb.get("fill_quality") or 0.0)
            avg_slippage = ((old_avg_slippage * old_cycles) + slippage) / max(cycles_observed, 1)
            avg_fill = ((old_avg_fill * old_cycles) + fill_quality) / max(cycles_observed, 1)
            promotion_streak = old_streak + 1 if status == "execution_ok" else 0
            demotion_count = old_demotion_count + (1 if status in {"slippage_bad", "fill_quality_bad"} else 0)
            last_feedback_status = status or str(prev.get("last_feedback_status") or "unknown")
        else:
            cycles_observed = old_cycles
            shadow_runs = old_shadow_runs
            paper_runs = old_paper_runs
            avg_slippage = old_avg_slippage
            avg_fill = old_avg_fill
            promotion_streak = old_streak
            demotion_count = old_demotion_count
            last_feedback_status = str(prev.get("last_feedback_status") or "no_recent_evidence")

        report_rows.append(
            {
                "candidate_id": candidate_id,
                "experiment_id": str(fb.get("experiment_id") or row.get("experiment_id") or ""),
                "cycles_observed": cycles_observed,
                "shadow_runs": shadow_runs,
                "paper_runs": paper_runs,
                "avg_slippage_delta": round(avg_slippage, 4),
                "avg_fill_quality": round(avg_fill, 4),
                "promotion_streak": promotion_streak,
                "demotion_count": demotion_count,
                "last_feedback_status": last_feedback_status,
                "last_cycle_id": cycle_id,
            }
        )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": report_rows,
        "summary": {
            "candidate_count": len(report_rows),
            "feedback_seen": len(feedback_rows),
        },
    }
