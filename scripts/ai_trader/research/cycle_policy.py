from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping

from scripts.ai_trader.research.feedback_mapping import build_feedback_mapping


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


def _as_str_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _infer_next_cycle_id(cycle_id: str | None) -> str:
    text = str(cycle_id or "").strip()
    try:
        d = datetime.fromisoformat(text).date()
        return (d + timedelta(days=1)).isoformat()
    except Exception:
        return f"{text}-next" if text else "next-cycle"


def build_next_cycle_policy(
    bundle: Mapping[str, Any],
    *,
    cycle_id: str,
    next_cycle_id: str | None = None,
    task_outcome_index: Mapping[str, Any] | None = None,
    task_schedule_plan: Mapping[str, Any] | None = None,
    task_retry_report: Mapping[str, Any] | None = None,
    task_manifest: Mapping[str, Any] | None = None,
    execution_feedback: Mapping[str, Any] | None = None,
    candidate_lifecycle_index: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    b = _as_dict(bundle)
    decision = _as_dict(b.get("promotion_decision"))
    leaderboard = _as_list(b.get("leaderboard"))
    blocked_summary = _as_dict(b.get("blocked_summary"))
    next_actions = _as_list(_as_dict(b.get("next_cycle_recommendations")).get("actions"))

    suppress_markets: list[str] = []
    suppress_symbols: list[str] = []
    retest_experiments: list[Dict[str, Any]] = []
    promote_candidates: list[Dict[str, Any]] = []
    robustness_adjustments: list[Dict[str, Any]] = []

    for item in _as_list(decision.get("blocked_by_data")):
        row = _as_dict(item)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        retest_experiments.append(
            {
                "experiment_id": exp_id,
                "intent": "retest_data",
                "actions": ["widen_window", "drop_symbol_if_missing_data"],
            }
        )

    for item in _as_list(decision.get("blocked_by_robustness")):
        row = _as_dict(item)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        retest_experiments.append(
            {
                "experiment_id": exp_id,
                "intent": "retest_robustness",
                "actions": ["increase_folds", "reduce_complexity", "expand_regime_window"],
            }
        )
        robustness_adjustments.append(
            {
                "experiment_id": exp_id,
                "adjustments": {
                    "increase_folds": True,
                    "reduce_feature_count": True,
                    "window_expand_days": 5,
                },
            }
        )

    for winner in _as_list(decision.get("winners")):
        row = _as_dict(winner)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        promote_candidates.append(
            {
                "experiment_id": exp_id,
                "intent": "promote_candidate",
                "lineage_action": "lineage_expand",
            }
        )

    by_market = _as_dict(blocked_summary.get("by_market"))
    if by_market.get("options", 0) > 0 and by_market.get("stocks", 0) == 0:
        suppress_markets.append("options")

    for row in leaderboard:
        item = _as_dict(row)
        result = _as_dict(item.get("result"))
        qg = _as_dict(item.get("quality_gate"))
        reasons = set(_as_str_list(qg.get("reasons")))
        market = str(result.get("market") or "").strip().lower()
        symbols = _as_str_list(_as_dict(result.get("universe")).get("symbols"))
        if not symbols:
            symbols = _as_str_list(_as_dict(_as_dict(result.get("blocked_by")).get("window")).get("symbols"))
        if "min_trades_not_met" in reasons and market:
            for sym in symbols:
                suppress_symbols.append(f"{market}:{sym}")

    budget_allocation = {"stocks": 0.5, "options": 0.5}
    if suppress_markets:
        if "options" in suppress_markets:
            budget_allocation = {"stocks": 0.8, "options": 0.2}
        elif "stocks" in suppress_markets:
            budget_allocation = {"stocks": 0.2, "options": 0.8}

    feedback = build_feedback_mapping(
        cycle_id=cycle_id,
        evaluation_bundle=bundle,
        task_outcome_index=task_outcome_index,
        task_schedule_plan=task_schedule_plan,
        task_retry_report=task_retry_report,
        task_manifest=task_manifest,
        execution_feedback=execution_feedback,
    )
    task_patches = _as_list(feedback.get("task_patches"))
    carry_forward_tasks = _as_list(feedback.get("carry_forward_tasks"))
    suppressed_targets = _as_list(feedback.get("suppressed_targets"))
    mapped_promote_candidates = _as_list(feedback.get("promote_candidates"))
    mapped_retest_queue = _as_list(feedback.get("retest_queue"))
    execution_actions = _as_list(feedback.get("execution_actions"))
    lifecycle_rows = _as_list(_as_dict(candidate_lifecycle_index).get("candidates"))

    lifecycle_suppressed: list[Dict[str, Any]] = []
    lifecycle_patches: list[Dict[str, Any]] = []
    for row in lifecycle_rows:
        item = _as_dict(row)
        state = str(item.get("current_state") or "")
        market = str(item.get("market") or "").strip().lower()
        symbol = str(item.get("symbol") or "").strip().upper()
        strategy_family = str(item.get("strategy_family") or "")
        candidate_id = str(item.get("candidate_id") or "")
        experiment_id = str(item.get("experiment_id") or "")
        if state == "retired":
            lifecycle_suppressed.append(
                {
                    "source_task_id": "",
                    "source_experiment_id": experiment_id,
                    "source_outcome": "retired",
                    "market": market,
                    "symbol": symbol,
                    "strategy_family": strategy_family,
                    "action": "suppress_retired_candidate",
                    "reason": "candidate_retired",
                    "candidate_id": candidate_id,
                }
            )
        elif state == "demoted":
            lifecycle_patches.append(
                {
                    "action": "demoted_retest_only",
                    "source_task_id": "",
                    "source_experiment_id": experiment_id,
                    "source_outcome": "demoted",
                    "market": market,
                    "symbol": symbol,
                    "strategy_family": strategy_family,
                    "intent": "retest_robustness",
                    "patch": {
                        "execution_reality": {
                            "promotion_cap": "shadow_only",
                        }
                    },
                    "priority": 89,
                    "candidate_id": candidate_id,
                }
            )
    suppressed_targets.extend(lifecycle_suppressed)
    task_patches.extend(lifecycle_patches)

    return {
        "source_cycle_id": cycle_id,
        "target_cycle_id": next_cycle_id or _infer_next_cycle_id(cycle_id),
        "generated_at": datetime.utcnow().isoformat(),
        "suppressions": {
            "markets": sorted(set(suppress_markets)),
            "symbols": sorted(set(suppress_symbols)),
        },
        "retest_experiments": retest_experiments,
        "promote_candidates": promote_candidates,
        "robustness_adjustments": robustness_adjustments,
        "planner_actions": next_actions,
        "budget_allocation": budget_allocation,
        "task_patches": task_patches,
        "carry_forward_tasks": carry_forward_tasks,
        "suppressed_targets": suppressed_targets,
        "promote_candidates": mapped_promote_candidates or promote_candidates,
        "retest_queue": mapped_retest_queue,
        "execution_feedback": _as_dict(execution_feedback),
        "execution_actions": execution_actions,
        "candidate_lifecycle": _as_dict(candidate_lifecycle_index),
        "feedback_mapping_report": _as_dict(feedback.get("feedback_mapping_report")),
    }


def persist_next_cycle_policy(
    bundle: Mapping[str, Any],
    *,
    output_dir: str | Path,
    cycle_id: str,
    next_cycle_id: str | None = None,
    task_outcome_index: Mapping[str, Any] | None = None,
    task_schedule_plan: Mapping[str, Any] | None = None,
    task_retry_report: Mapping[str, Any] | None = None,
    task_manifest: Mapping[str, Any] | None = None,
    execution_feedback: Mapping[str, Any] | None = None,
    candidate_lifecycle_index: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    policy = build_next_cycle_policy(
        bundle,
        cycle_id=cycle_id,
        next_cycle_id=next_cycle_id,
        task_outcome_index=task_outcome_index,
        task_schedule_plan=task_schedule_plan,
        task_retry_report=task_retry_report,
        task_manifest=task_manifest,
        execution_feedback=execution_feedback,
        candidate_lifecycle_index=candidate_lifecycle_index,
    )
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "next_cycle_policy.json"
    path.write_text(json.dumps(policy, indent=2, ensure_ascii=False), encoding="utf-8")
    feedback_path = out / "feedback_mapping_report.json"
    feedback_path.write_text(
        json.dumps(_as_dict(policy.get("feedback_mapping_report")), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"policy": policy, "path": str(path), "feedback_mapping_report_path": str(feedback_path)}


def load_cycle_policy(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
