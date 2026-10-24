from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.agent import ResearchAgent
from scripts.ai_trader.research.agent_tasking import compile_agent_tasks
from scripts.ai_trader.research.candidate_pool import update_candidate_pool_from_evaluation
from scripts.ai_trader.research.cycle_policy import build_next_cycle_policy


def _evaluation_bundle() -> dict:
    return {
        "leaderboard": [
            {
                "experiment_id": "exp-data-blocked",
                "result": {
                    "experiment_id": "exp-data-blocked",
                    "market": "stocks",
                    "strategy_family": "stocks_scalp_seed",
                    "universe": {"symbols": ["QQQ"]},
                    "lineage": {"family_id": "stocks_scalp_seed", "mutation_type": "seed"},
                },
                "quality_gate": {"reasons": ["data_readiness_blocked"]},
            },
            {
                "experiment_id": "exp-robust-fail",
                "result": {
                    "experiment_id": "exp-robust-fail",
                    "market": "options",
                    "strategy_family": "options_scalp_seed",
                    "universe": {"symbols": ["SPY"], "underlying": "SPY"},
                    "lineage": {"family_id": "options_scalp_seed", "mutation_type": "seed"},
                },
                "quality_gate": {"reasons": ["robustness_failed"]},
            },
            {
                "experiment_id": "exp-winner",
                "result": {
                    "experiment_id": "exp-winner",
                    "market": "stocks",
                    "strategy_family": "stocks_scalp_seed",
                    "universe": {"symbols": ["IWM"]},
                    "lineage": {
                        "family_id": "stocks_scalp_seed",
                        "mutation_type": "seed",
                        "parent_experiment_id": "exp-parent",
                    },
                },
                "quality_gate": {"reasons": []},
            },
        ],
        "blocked_summary": {"by_market": {"stocks": 1, "options": 0}},
        "quality_gate_breakdown": {"by_reason": {"data_readiness_blocked": 1, "robustness_failed": 1}},
        "next_cycle_recommendations": {"actions": ["suppress_symbol:stocks:QQQ", "retest_robustness:exp-robust-fail"]},
        "promotion_decision": {
            "winners": [{"experiment_id": "exp-winner", "score": 95, "tier": "A"}],
            "retest_queue": [{"experiment_id": "exp-robust-fail", "reason": "robustness_failed", "score": 50}],
            "rejected": [{"experiment_id": "exp-data-blocked", "reason": "data_readiness_blocked"}],
            "blocked_by_data": [{"experiment_id": "exp-data-blocked"}],
            "blocked_by_robustness": [{"experiment_id": "exp-robust-fail"}],
        },
        "portfolio_evaluation": {"selected": ["exp-winner"], "rejected": ["exp-data-blocked"]},
    }


def _task_outcomes() -> dict:
    return {
        "cycle_id": "2026-04-08",
        "outcomes": [
            {"task_id": "task-data", "experiment_id": "exp-data-blocked", "status": "blocked"},
            {"task_id": "task-robust", "experiment_id": "exp-robust-fail", "status": "completed"},
            {"task_id": "task-win", "experiment_id": "exp-winner", "status": "completed"},
        ],
    }


def _task_manifest() -> dict:
    return {
        "cycle_id": "2026-04-08",
        "tasks": [
            {"task_id": "task-data", "task_patch": {"market": "stocks", "strategy_family": "stocks_scalp_seed", "universe": {"symbols": ["QQQ"]}}},
            {"task_id": "task-robust", "task_patch": {"market": "options", "strategy_family": "options_scalp_seed", "universe": {"symbols": ["SPY"]}}},
            {"task_id": "task-win", "task_patch": {"market": "stocks", "strategy_family": "stocks_scalp_seed", "universe": {"symbols": ["IWM"]}}},
        ],
    }


def test_bundle_policy_task_spec_convergence_and_pool_update(tmp_path: Path) -> None:
    bundle = _evaluation_bundle()
    cycle_policy = build_next_cycle_policy(
        bundle,
        cycle_id="2026-04-08",
        task_outcome_index=_task_outcomes(),
        task_schedule_plan={"entries": []},
        task_retry_report={"tasks": []},
        task_manifest=_task_manifest(),
        execution_feedback={"items": []},
        candidate_lifecycle_index={"candidates": []},
    )

    # 1) blocked_by_data -> suppress
    assert any(str(x.get("action")) == "suppress_symbol" for x in cycle_policy.get("suppressed_targets", []))
    # 2) blocked_by_robustness -> retest with robustness patch
    robust_patch = next(
        (
            x for x in cycle_policy.get("task_patches", [])
            if str(x.get("source_experiment_id")) == "exp-robust-fail"
        ),
        {},
    )
    robust_payload = robust_patch.get("patch", {})
    assert robust_patch.get("intent") == "retest_robustness"
    assert robust_payload.get("robustness", {}).get("folds") == 8
    assert robust_payload.get("robustness", {}).get("window_expand_days") == 5
    assert robust_payload.get("parameters", {}).get("reduce_complexity") is True
    # 3) winner -> lineage/promote
    assert any(str(x.get("source_experiment_id")) == "exp-winner" for x in cycle_policy.get("promote_candidates", []))

    seed_specs = [
        {
            "market": "stocks",
            "strategy_family": "stocks_scalp_seed",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "module_selection": {"features": ["price_action", "volume_spike"]},
            "parameters": {"max_feature_count": 6},
            "robustness": {"required_checks": ["walk_forward"]},
        },
        {
            "market": "options",
            "strategy_family": "options_scalp_seed",
            "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
            "universe": {"symbols": ["SPY"], "underlying": "SPY"},
            "strategy_ref": {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
            "module_selection": {"features": ["delta", "gamma", "iv_rank"]},
            "parameters": {"max_feature_count": 6},
            "robustness": {"required_checks": ["walk_forward"]},
        },
        {
            "market": "stocks",
            "strategy_family": "stocks_scalp_seed",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["IWM"]},
            "strategy_ref": {"template_id": "iwm_intraday_core"},
            "module_selection": {"features": ["price_action", "trend"]},
            "parameters": {"max_feature_count": 5},
            "robustness": {"required_checks": ["walk_forward"]},
        },
    ]
    tasks = compile_agent_tasks(
        cycle_id="2026-04-09",
        cycle_policy=cycle_policy,
        seed_specs=seed_specs,
        input_refs=["blocked_summary.json", "quality_gate_breakdown.json", "next_cycle_recommendations.json", "promotion_decision.json", "portfolio_evaluation.json"],
    )
    strategy_tasks = [t for t in tasks if str(t.get("task_type")) == "strategy_experiment"]
    assert strategy_tasks

    # 4) next-cycle AgentTask has real diff
    blocked_task = next((t for t in strategy_tasks if str((t.get("task_patch") or {}).get("market")) == "stocks" and "QQQ" in json.dumps(t.get("task_patch") or {})), {})
    assert blocked_task.get("blocked_by")
    assert str(blocked_task.get("research_intent")) == "retest_data"

    robust_task = next(
        (
            t
            for t in strategy_tasks
            if str((t.get("task_patch") or {}).get("market")) == "options"
            and str((t.get("derived_from") or {}).get("source_experiment_id") or "") == "exp-robust-fail"
        ),
        {},
    )
    robust_task_patch = robust_task.get("task_patch", {})
    assert str(robust_task.get("research_intent")) == "retest_robustness"
    assert robust_task_patch.get("robustness", {}).get("folds") == 8

    winner_task = next((t for t in strategy_tasks if "IWM" in json.dumps(t.get("task_patch") or {})), {})
    assert str(winner_task.get("research_intent")) in {"lineage_expand", "promote_candidate"}
    assert winner_task.get("derived_from", {}).get("source_experiment_id") == "exp-winner"

    # 5) next-cycle ExperimentSpec has real diff
    agent = ResearchAgent(llm_client=object())  # no provider pool needed for this helper
    next_specs = agent.build_experiment_specs_from_tasks(strategy_tasks, cycle_id="2026-04-09")
    robust_spec = next((s for s in next_specs if str(s.get("market")) == "options"), {})
    blocked_spec = next((s for s in next_specs if str(s.get("market")) == "stocks" and "QQQ" in json.dumps(s)), {})
    assert robust_spec.get("robustness", {}).get("folds") == 8
    assert robust_spec.get("parameters", {}).get("reduce_complexity") is True
    assert blocked_spec.get("blocked_by")
    assert str(blocked_spec.get("research_intent")) == "retest_data"

    # 6) candidate_pool / lineage updated
    artifacts = update_candidate_pool_from_evaluation(
        cycle_id="2026-04-08",
        evaluation_bundle=bundle,
        state_dir=tmp_path / "state",
    )
    cp = json.loads(Path(artifacts["candidate_pool_json"]).read_text(encoding="utf-8"))
    li = json.loads(Path(artifacts["lineage_index_json"]).read_text(encoding="utf-8"))
    assert any(str(x.get("experiment_id")) == "exp-winner" and str(x.get("status")) == "promoted" for x in cp.get("candidates", []))
    assert any(str(x.get("experiment_id")) == "exp-data-blocked" and str(x.get("status")) == "rejected" for x in cp.get("candidates", []))
    assert any(str(x.get("experiment_id")) == "exp-robust-fail" and str(x.get("status")) == "retest" for x in li.get("records", []))
    assert any(str(x.get("experiment_id")) == "exp-data-blocked" and str(x.get("status")) == "rejected" for x in li.get("records", []))
