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


def build_candidate_gate(
    *,
    cycle_id: str,
    lifecycle_index: Mapping[str, Any],
    promotion_gate: Mapping[str, Any],
    execution_feedback: Mapping[str, Any],
    retention_report: Mapping[str, Any],
    min_cycles_for_promote: int = 1,
) -> Dict[str, Any]:
    lifecycle_rows = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(lifecycle_index).get("candidates"))
        if str(_as_dict(x).get("candidate_id") or "")
    }
    promotion_rows = {
        str(_as_dict(x).get("experiment_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(promotion_gate).get("items"))
        if str(_as_dict(x).get("experiment_id") or "")
    }
    feedback_rows = {
        str(_as_dict(x).get("candidate_id") or _as_dict(x).get("experiment_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(execution_feedback).get("items"))
        if str(_as_dict(x).get("candidate_id") or _as_dict(x).get("experiment_id") or "")
    }
    retention_rows = {
        str(_as_dict(x).get("candidate_id") or ""): _as_dict(x)
        for x in _as_list(_as_dict(retention_report).get("items"))
        if str(_as_dict(x).get("candidate_id") or "")
    }

    all_ids = sorted(set(list(lifecycle_rows.keys()) + list(promotion_rows.keys()) + list(feedback_rows.keys())))
    decisions: list[Dict[str, Any]] = []
    for candidate_id in all_ids:
        prev = _as_dict(lifecycle_rows.get(candidate_id))
        old_state = str(prev.get("current_state") or "research_only")
        stale_cycles = int(prev.get("stale_cycles") or 0)
        promote = _as_dict(promotion_rows.get(candidate_id))
        feedback = _as_dict(feedback_rows.get(candidate_id))
        retention = _as_dict(retention_rows.get(candidate_id))
        cycles_observed = int(retention.get("cycles_observed") or 0)
        demotion_count = int(retention.get("demotion_count") or 0)
        feedback_status = str(feedback.get("feedback_status") or "")
        gate_status = str(promote.get("gate_status") or "")
        promotion_target = str(promote.get("promotion_target") or "")
        has_recent_feedback = bool(feedback)

        new_state = old_state
        action = "hold"
        reasons: list[str] = []
        source_feedback = feedback_status or "no_recent_evidence"

        if has_recent_feedback:
            stale_cycles = 0
        else:
            stale_cycles += 1

        if demotion_count >= 2 and feedback_status in {"slippage_bad", "fill_quality_bad"}:
            new_state = "retired"
            action = "retire"
            reasons.append("repeated_failures")
        elif not has_recent_feedback and stale_cycles >= 2:
            new_state = "retired"
            action = "retire"
            reasons.append("no_recent_evidence")
        elif not has_recent_feedback and stale_cycles == 1:
            action = "stale"
            reasons.append("no_recent_evidence")
        elif feedback_status in {"slippage_bad", "fill_quality_bad"}:
            if old_state in {"paper_active", "paper_candidate"}:
                new_state = "demoted"
                action = "demote"
                reasons.append("execution_quality_degraded")
            else:
                new_state = "shadow_candidate"
                action = "hold"
                reasons.append("execution_quality_watch")
        elif feedback_status == "execution_ok" and gate_status == "passed" and cycles_observed >= min_cycles_for_promote:
            action = "promote"
            reasons.append("execution_ok_gate_pass_min_cycles")
            if promotion_target == "shadow_candidate":
                new_state = "shadow_active"
            elif promotion_target == "paper_candidate":
                new_state = "paper_active" if old_state in {"paper_candidate", "paper_active"} else "paper_candidate"
            elif old_state == "research_only":
                new_state = "shadow_candidate"
        elif gate_status == "passed" and old_state == "research_only":
            new_state = "shadow_candidate"
            action = "promote"
            reasons.append("gate_pass_initial_promotion")

        decisions.append(
            {
                "candidate_id": candidate_id,
                "experiment_id": str(feedback.get("experiment_id") or promote.get("experiment_id") or prev.get("experiment_id") or ""),
                "previous_state": old_state,
                "next_state": new_state,
                "changed": new_state != old_state,
                "gate_action": action,
                "stale_cycles": stale_cycles,
                "reasons": reasons or ["hold_state"],
                "source_cycle": cycle_id,
                "source_candidate": candidate_id,
                "source_feedback": source_feedback,
                "market": str(feedback.get("market") or prev.get("market") or "").strip().lower(),
                "symbol": str(feedback.get("symbol") or prev.get("symbol") or "").strip().upper(),
                "strategy_family": str(feedback.get("strategy_family") or prev.get("strategy_family") or ""),
            }
        )

    return {
        "cycle_id": cycle_id,
        "generated_at": _iso_now(),
        "items": decisions,
        "summary": {
            "promote": sum(1 for x in decisions if x.get("gate_action") == "promote"),
            "demote": sum(1 for x in decisions if x.get("gate_action") == "demote"),
            "retire": sum(1 for x in decisions if x.get("gate_action") == "retire"),
            "stale": sum(1 for x in decisions if x.get("gate_action") == "stale"),
        },
    }
