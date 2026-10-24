from __future__ import annotations

from datetime import datetime
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


def _symbol_from_result(result: Mapping[str, Any]) -> str:
    universe = _as_dict(result.get("universe"))
    symbols = _as_list(universe.get("symbols"))
    if symbols:
        return str(symbols[0]).upper()
    underlying = universe.get("underlying")
    if underlying:
        return str(underlying).upper()
    blocked_by = _as_dict(result.get("blocked_by"))
    sym = blocked_by.get("symbol")
    return str(sym).upper() if sym else ""


def build_feedback_mapping(
    *,
    cycle_id: str,
    evaluation_bundle: Mapping[str, Any],
    task_outcome_index: Mapping[str, Any] | None = None,
    task_schedule_plan: Mapping[str, Any] | None = None,
    task_retry_report: Mapping[str, Any] | None = None,
    task_manifest: Mapping[str, Any] | None = None,
    execution_feedback: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    bundle = _as_dict(evaluation_bundle)
    decision = _as_dict(bundle.get("promotion_decision"))
    leaderboard = _as_list(bundle.get("leaderboard"))
    outcomes = _as_list(_as_dict(task_outcome_index).get("outcomes"))
    schedule_entries = _as_list(_as_dict(task_schedule_plan).get("entries"))
    retry_rows = _as_list(_as_dict(task_retry_report).get("tasks"))
    manifest_tasks = _as_list(_as_dict(task_manifest).get("tasks"))
    execution_rows = _as_list(_as_dict(execution_feedback).get("items"))

    task_by_id = {
        str(_as_dict(t).get("task_id") or ""): _as_dict(t)
        for t in manifest_tasks
        if str(_as_dict(t).get("task_id") or "")
    }
    outcome_by_exp = {
        str(_as_dict(x).get("experiment_id") or ""): _as_dict(x)
        for x in outcomes
        if str(_as_dict(x).get("experiment_id") or "")
    }
    retry_by_task = {
        str(_as_dict(x).get("task_id") or ""): _as_dict(x)
        for x in retry_rows
        if str(_as_dict(x).get("task_id") or "")
    }
    schedule_by_task = {
        str(_as_dict(x).get("task_id") or ""): _as_dict(x)
        for x in schedule_entries
        if str(_as_dict(x).get("task_id") or "")
    }
    lb_by_exp = {
        str(_as_dict(x).get("experiment_id") or ""): _as_dict(x)
        for x in leaderboard
        if str(_as_dict(x).get("experiment_id") or "")
    }

    task_patches: list[Dict[str, Any]] = []
    carry_forward_tasks: list[Dict[str, Any]] = []
    suppressed_targets: list[Dict[str, Any]] = []
    promote_candidates: list[Dict[str, Any]] = []
    retest_queue: list[Dict[str, Any]] = []
    execution_actions: list[Dict[str, Any]] = []
    records: list[Dict[str, Any]] = []

    # completed + passed -> promote_candidate / lineage_expand
    for winner in _as_list(decision.get("winners")):
        row = _as_dict(winner)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        outcome = _as_dict(outcome_by_exp.get(exp_id))
        source_task_id = str(outcome.get("task_id") or "")
        lb = _as_dict(lb_by_exp.get(exp_id))
        result = _as_dict(lb.get("result"))
        market = str(result.get("market") or "").strip().lower()
        strategy_family = str(result.get("strategy_family") or "")
        symbol = _symbol_from_result(result)
        patch = {
            "market": market,
            "strategy_family": strategy_family,
            "lineage": {
                "parent_experiment_id": exp_id,
                "mutation_type": "lineage_expand",
                "family_id": _as_dict(result.get("lineage")).get("family_id") or strategy_family,
            },
        }
        promote_row = {
            "source_task_id": source_task_id,
            "source_experiment_id": exp_id,
            "source_outcome": outcome.get("status") or "completed",
            "market": market,
            "symbol": symbol,
            "strategy_family": strategy_family,
            "patch": patch,
            "intent": "lineage_expand",
        }
        promote_candidates.append(promote_row)
        task_patches.append(
            {
                **promote_row,
                "action": "lineage_expand",
                "priority": 100,
            }
        )
        records.append(
            {
                "rule": "completed_passed",
                "action": "lineage_expand",
                "source_task_id": source_task_id,
                "source_experiment_id": exp_id,
                "source_outcome": promote_row["source_outcome"],
            }
        )

    # blocked_by_data -> suppress_symbol / defer_market
    for blocked in _as_list(decision.get("blocked_by_data")):
        row = _as_dict(blocked)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        outcome = _as_dict(outcome_by_exp.get(exp_id))
        source_task_id = str(outcome.get("task_id") or "")
        lb = _as_dict(lb_by_exp.get(exp_id))
        result = _as_dict(lb.get("result"))
        market = str(result.get("market") or "").strip().lower()
        symbol = _symbol_from_result(result)
        suppressed_row = {
            "source_task_id": source_task_id,
            "source_experiment_id": exp_id,
            "source_outcome": outcome.get("status") or "blocked",
            "market": market,
            "symbol": symbol,
            "action": "suppress_symbol",
            "reason": "blocked_by_data",
        }
        suppressed_targets.append(suppressed_row)
        task_patches.append(
            {
                **suppressed_row,
                "intent": "retest_data",
                "priority": 85,
                "patch": {"blocked_by": {"type": "policy_suppression", "reason": "blocked_by_data"}},
            }
        )
        records.append(
            {
                "rule": "blocked_by_data",
                "action": "suppress_symbol",
                "source_task_id": source_task_id,
                "source_experiment_id": exp_id,
                "source_outcome": suppressed_row["source_outcome"],
            }
        )

    # blocked_by_robustness -> retest_robustness
    for blocked in _as_list(decision.get("blocked_by_robustness")):
        row = _as_dict(blocked)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        outcome = _as_dict(outcome_by_exp.get(exp_id))
        source_task_id = str(outcome.get("task_id") or "")
        lb = _as_dict(lb_by_exp.get(exp_id))
        result = _as_dict(lb.get("result"))
        market = str(result.get("market") or "").strip().lower()
        strategy_family = str(result.get("strategy_family") or "")
        symbol = _symbol_from_result(result)
        retest_row = {
            "source_task_id": source_task_id,
            "source_experiment_id": exp_id,
            "source_outcome": outcome.get("status") or "blocked",
            "intent": "retest_robustness",
            "action": "retest_robustness",
            "market": market,
            "symbol": symbol,
            "strategy_family": strategy_family,
        }
        retest_queue.append(retest_row)
        task_patches.append(
            {
                **retest_row,
                "priority": 90,
                "patch": {
                    "research_intent": "retest_robustness",
                    "robustness": {
                        "required_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
                        "folds": 8,
                        "window_expand_days": 5,
                        "regime_split_mode": "expanded",
                    },
                    "parameters": {
                        "reduce_complexity": True,
                        "max_feature_count": 3,
                    },
                },
            }
        )
        records.append(
            {
                "rule": "blocked_by_robustness",
                "action": "retest_robustness",
                "source_task_id": source_task_id,
                "source_experiment_id": exp_id,
                "source_outcome": retest_row["source_outcome"],
            }
        )

    # retest_queue (quality-gate / robustness fail) -> retest_robustness
    for blocked in _as_list(decision.get("retest_queue")):
        row = _as_dict(blocked)
        exp_id = str(row.get("experiment_id") or "")
        if not exp_id:
            continue
        # already covered above
        if any(str(_as_dict(x).get("source_experiment_id") or "") == exp_id for x in retest_queue):
            continue
        outcome = _as_dict(outcome_by_exp.get(exp_id))
        source_task_id = str(outcome.get("task_id") or "")
        lb = _as_dict(lb_by_exp.get(exp_id))
        result = _as_dict(lb.get("result"))
        market = str(result.get("market") or "").strip().lower()
        strategy_family = str(result.get("strategy_family") or "")
        symbol = _symbol_from_result(result)
        retest_row = {
            "source_task_id": source_task_id,
            "source_experiment_id": exp_id,
            "source_outcome": outcome.get("status") or "completed",
            "intent": "retest_robustness",
            "action": "retest_robustness",
            "market": market,
            "symbol": symbol,
            "strategy_family": strategy_family,
        }
        retest_queue.append(retest_row)
        task_patches.append(
            {
                **retest_row,
                "priority": 90,
                "patch": {
                    "research_intent": "retest_robustness",
                    "robustness": {
                        "required_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
                        "folds": 8,
                        "window_expand_days": 5,
                        "regime_split_mode": "expanded",
                    },
                    "parameters": {
                        "reduce_complexity": True,
                        "max_feature_count": 3,
                    },
                },
            }
        )
        records.append(
            {
                "rule": "retest_queue",
                "action": "retest_robustness",
                "source_task_id": source_task_id,
                "source_experiment_id": exp_id,
                "source_outcome": retest_row["source_outcome"],
            }
        )

    # failed_retry_exhausted -> defer_with_reason
    for task_id, retry in retry_by_task.items():
        if str(retry.get("defer_reason") or "") != "retry_exhausted":
            continue
        task = _as_dict(task_by_id.get(task_id))
        defer_row = {
            "source_task_id": task_id,
            "source_experiment_id": str(retry.get("experiment_id") or ""),
            "source_outcome": str(retry.get("final_status") or "failed"),
            "action": "defer_with_reason",
            "reason": "failed_retry_exhausted",
            "task_patch": _as_dict(task.get("task_patch")),
            "intent": "retest_data",
        }
        params = _as_dict(_as_dict(defer_row["task_patch"]).get("parameters"))
        params["carry_forward_from_task"] = task_id
        defer_row["task_patch"]["parameters"] = params
        family = str(_as_dict(defer_row["task_patch"]).get("strategy_family") or "")
        if family:
            defer_row["task_patch"]["strategy_family"] = f"{family}_carry"
        carry_forward_tasks.append(defer_row)
        records.append(
            {
                "rule": "failed_retry_exhausted",
                "action": "defer_with_reason",
                "source_task_id": task_id,
                "source_experiment_id": defer_row["source_experiment_id"],
                "source_outcome": defer_row["source_outcome"],
            }
        )

    # deferred_by_budget -> carry_forward
    for task_id, schedule in schedule_by_task.items():
        decision_text = str(schedule.get("decision") or "")
        reason = str(schedule.get("reason") or "")
        if decision_text != "deferred":
            continue
        if "budget" not in reason and "max_scheduled_tasks" not in reason:
            continue
        task = _as_dict(task_by_id.get(task_id))
        carry_row = {
            "source_task_id": task_id,
            "source_experiment_id": "",
            "source_outcome": "deferred_by_budget",
            "action": "carry_forward",
            "reason": reason or "deferred_by_budget",
            "task_patch": _as_dict(task.get("task_patch")),
            "intent": str(task.get("research_intent") or "seed"),
        }
        params = _as_dict(_as_dict(carry_row["task_patch"]).get("parameters"))
        params["carry_forward_from_task"] = task_id
        carry_row["task_patch"]["parameters"] = params
        family = str(_as_dict(carry_row["task_patch"]).get("strategy_family") or "")
        if family:
            carry_row["task_patch"]["strategy_family"] = f"{family}_carry"
        carry_forward_tasks.append(carry_row)
        records.append(
            {
                "rule": "deferred_by_budget",
                "action": "carry_forward",
                "source_task_id": task_id,
                "source_experiment_id": "",
                "source_outcome": "deferred_by_budget",
            }
        )

    # execution feedback -> explicit next-cycle task patches
    for row in execution_rows:
        fb = _as_dict(row)
        exp_id = str(fb.get("experiment_id") or "")
        task_id = str(fb.get("source_task_id") or _as_dict(outcome_by_exp.get(exp_id)).get("task_id") or "")
        status = str(fb.get("feedback_status") or "")
        market = str(fb.get("market") or "").strip().lower()
        symbol = str(fb.get("symbol") or "").strip().upper()
        strategy_family = str(fb.get("strategy_family") or "")

        if status == "execution_ok":
            action = {
                "action": "keep_candidate",
                "source_task_id": task_id,
                "source_experiment_id": exp_id,
                "source_outcome": "execution_ok",
                "market": market,
                "symbol": symbol,
                "strategy_family": strategy_family,
                "intent": "promote_candidate",
                "patch": {
                    "execution_reality": {
                        "shadow_feedback": "execution_ok",
                    }
                },
                "priority": 98,
            }
        elif status == "slippage_bad":
            action = {
                "action": "tighten_execution_constraints",
                "source_task_id": task_id,
                "source_experiment_id": exp_id,
                "source_outcome": "slippage_bad",
                "market": market,
                "symbol": symbol,
                "strategy_family": strategy_family,
                "intent": "retest_robustness",
                "patch": {
                    "execution_reality": {
                        "max_slippage_bps": 6.0,
                        "use_limit_order_only": True,
                    }
                },
                "priority": 92,
            }
        elif status == "fill_quality_bad":
            action = {
                "action": "downgrade_or_retest",
                "source_task_id": task_id,
                "source_experiment_id": exp_id,
                "source_outcome": "fill_quality_bad",
                "market": market,
                "symbol": symbol,
                "strategy_family": strategy_family,
                "intent": "retest_robustness",
                "patch": {
                    "execution_reality": {
                        "min_fill_ratio": 0.9,
                        "max_orders_per_day": 10,
                    }
                },
                "priority": 91,
            }
        else:
            continue

        task_patches.append(action)
        execution_actions.append(action)
        records.append(
            {
                "rule": "execution_feedback",
                "action": str(action.get("action") or ""),
                "source_task_id": task_id,
                "source_experiment_id": exp_id,
                "source_outcome": str(action.get("source_outcome") or ""),
            }
        )

    # dedupe carry-forward by source_task_id
    deduped_cf: list[Dict[str, Any]] = []
    seen_cf: set[str] = set()
    for row in carry_forward_tasks:
        tid = str(row.get("source_task_id") or "")
        if not tid or tid in seen_cf:
            continue
        seen_cf.add(tid)
        deduped_cf.append(row)

    generated_at = datetime.utcnow().isoformat()
    return {
        "generated_at": generated_at,
        "task_patches": task_patches,
        "carry_forward_tasks": deduped_cf,
        "suppressed_targets": suppressed_targets,
        "promote_candidates": promote_candidates,
        "retest_queue": retest_queue,
        "execution_actions": execution_actions,
        "feedback_mapping_report": {
            "cycle_id": cycle_id,
            "generated_at": generated_at,
            "records": records,
            "summary": {
                "task_patches": len(task_patches),
                "carry_forward_tasks": len(deduped_cf),
                "suppressed_targets": len(suppressed_targets),
                "promote_candidates": len(promote_candidates),
                "retest_queue": len(retest_queue),
                "execution_actions": len(execution_actions),
            },
        },
    }
