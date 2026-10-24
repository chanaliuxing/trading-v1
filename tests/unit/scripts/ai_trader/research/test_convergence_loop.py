from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.ai_trader.research.agent import ResearchAgent
from scripts.ai_trader.research.agent_tasking import compile_agent_tasks
from scripts.ai_trader.research.candidate_pool import update_candidate_pool_from_evaluation
from scripts.ai_trader.research.cycle_policy import build_next_cycle_policy
from scripts.ai_trader.research.policy import (
    AutoEvolutionRules,
    ExperimentBudget,
    ResearchPolicy,
    SafetyLimits,
    StrategyFocusRules,
)
from scripts.ai_trader.research.state import BudgetState, Budgets, ResearchState


def _state() -> ResearchState:
    return ResearchState(
        timestamp="2026-04-05T00:00:00+00:00",
        policy_version="test-v1",
        budgets=Budgets(
            compute_hours=BudgetState(total=12.0, used=0.0),
            tokens=BudgetState(total=1_000_000, used=0.0),
        ),
        strategies={},
        skills={},
        immune_cases=[],
        immune_actions=[],
        experiments=[],
        mutable_targets={},
    )


def _policy() -> ResearchPolicy:
    return ResearchPolicy(
        version="test-v1",
        objectives={},
        experiment_budget=ExperimentBudget(
            daily_compute_hours=12.0,
            max_parallel_experiments=5,
            exploration_ratio=0.2,
        ),
        strategy_focus_rules=StrategyFocusRules(
            min_data_points_for_eval=50,
            min_oos_sharpe_for_scaleup=0.7,
        ),
        safety_limits=SafetyLimits(max_unreviewed_patches_per_day=1, forbidden_targets=[]),
        auto_evolution=AutoEvolutionRules(
            allow_kinds=[],
            require_human_review_for=[],
            max_auto_patches_per_day=0,
            max_files_touched=1,
        ),
    )


def test_cycle_policy_compiles_machine_actions() -> None:
    bundle = {
        "leaderboard": [],
        "blocked_summary": {"by_market": {"options": 1, "stocks": 0}},
        "next_cycle_recommendations": {
            "actions": [{"action": "window_expand", "reason": "min_trades_not_met", "priority": "high"}]
        },
        "promotion_decision": {
            "winners": [{"experiment_id": "exp-good-001", "tier": "candidate", "score": 1.2}],
            "blocked_by_data": [{"experiment_id": "exp-data-001"}],
            "blocked_by_robustness": [{"experiment_id": "exp-rob-001"}],
        },
    }
    task_manifest = {
        "tasks": [
            {"task_id": "task-pass", "task_patch": {"market": "stocks", "universe": {"symbols": ["QQQ"]}, "strategy_family": "stocks_scalp"}},
            {"task_id": "task-data", "task_patch": {"market": "options", "universe": {"symbols": ["SPY"]}, "strategy_family": "options_scalp"}},
            {"task_id": "task-fail", "task_patch": {"market": "stocks", "universe": {"symbols": ["IWM"]}, "strategy_family": "stocks_scalp"}},
        ]
    }
    out = build_next_cycle_policy(
        bundle,
        cycle_id="2026-04-05",
        task_outcome_index={
            "outcomes": [
                {"task_id": "task-pass", "experiment_id": "exp-good-001", "status": "completed"},
                {"task_id": "task-data", "experiment_id": "exp-data-001", "status": "blocked"},
                {"task_id": "task-fail", "experiment_id": "exp-rob-001", "status": "failed"},
            ]
        },
        task_schedule_plan={
            "entries": [{"task_id": "task-fail", "decision": "deferred", "reason": "cycle_compute_budget_exceeded"}]
        },
        task_retry_report={
            "tasks": [{"task_id": "task-fail", "experiment_id": "exp-rob-001", "final_status": "failed", "defer_reason": "retry_exhausted"}]
        },
        task_manifest=task_manifest,
    )
    assert out["target_cycle_id"] == "2026-04-06"
    assert "options" in out["suppressions"]["markets"]
    assert out["retest_experiments"]
    assert out["promote_candidates"]
    assert out["task_patches"]
    assert out["carry_forward_tasks"]
    assert out["suppressed_targets"]
    assert out["retest_queue"]
    patch = out["task_patches"][0]
    assert "source_task_id" in patch
    assert "source_experiment_id" in patch
    assert "source_outcome" in patch


def test_task_compiler_is_stable_for_same_policy_and_seed() -> None:
    policy = {"source_cycle_id": "2026-04-04", "planner_actions": [], "suppressions": {"markets": [], "symbols": []}}
    seed_specs = [
        {
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "module_selection": {"features": ["a"], "gates": ["g"]},
            "parameters": {},
            "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
            "labels_and_ml": {},
            "robustness": {},
            "execution_reality": {},
            "sizing": {},
            "lineage": {},
            "acceptance": {},
            "research_intent": "seed",
        }
    ]
    tasks_a = compile_agent_tasks(cycle_id="2026-04-05", cycle_policy=policy, seed_specs=seed_specs)
    tasks_b = compile_agent_tasks(cycle_id="2026-04-05", cycle_policy=policy, seed_specs=seed_specs)
    ids_a = [t["task_id"] for t in tasks_a]
    ids_b = [t["task_id"] for t in tasks_b]
    assert ids_a == ids_b


def test_agent_consumes_agent_tasks_and_builds_stable_specs() -> None:
    cycle_policy = {
        "source_cycle_id": "2026-04-04",
        "target_cycle_id": "2026-04-05",
        "suppressions": {"markets": ["options"], "symbols": []},
        "retest_experiments": [{"experiment_id": "exp-rob-001", "intent": "retest_robustness"}],
        "promote_candidates": [{"experiment_id": "exp-good-001", "intent": "promote_candidate"}],
        "planner_actions": [],
    }
    seed_specs = [
        {
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "module_selection": {"features": ["a"], "gates": ["g"]},
            "parameters": {},
            "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
            "labels_and_ml": {},
            "robustness": {},
            "execution_reality": {},
            "sizing": {},
            "lineage": {},
            "acceptance": {},
            "research_intent": "seed",
        }
    ]
    tasks = compile_agent_tasks(cycle_id="2026-04-05", cycle_policy=cycle_policy, seed_specs=seed_specs)
    agent = ResearchAgent(llm_client=SimpleNamespace(chat=lambda **_: None))
    specs_a = agent.build_experiment_specs_from_tasks(tasks, cycle_id="2026-04-05")
    specs_b = agent.build_experiment_specs_from_tasks(tasks, cycle_id="2026-04-05")
    assert specs_a and specs_b
    assert specs_a[0]["task_id"] == tasks[0]["task_id"]
    assert specs_a[0]["experiment_id"] == specs_b[0]["experiment_id"]
    assert specs_a[0]["research_intent"] == tasks[0]["research_intent"]


def test_task_compiler_prioritizes_feedback_task_patches() -> None:
    cycle_policy = {
        "source_cycle_id": "2026-04-04",
        "task_patches": [
            {
                "action": "lineage_expand",
                "source_task_id": "task-pass",
                "source_experiment_id": "exp-pass-001",
                "source_outcome": "completed",
                "market": "stocks",
                "symbol": "QQQ",
                "intent": "lineage_expand",
                "patch": {"lineage": {"parent_experiment_id": "exp-pass-001", "mutation_type": "lineage_expand"}},
            }
        ],
        "carry_forward_tasks": [
            {
                "action": "carry_forward",
                "source_task_id": "task-carry",
                "source_experiment_id": "exp-carry-001",
                "source_outcome": "deferred_by_budget",
                "intent": "seed",
                "task_patch": {
                    "market": "stocks",
                    "strategy_family": "carry_family",
                    "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
                    "universe": {"symbols": ["IWM"]},
                    "strategy_ref": {"template_id": "qqq_intraday_core"},
                    "module_selection": {},
                    "parameters": {},
                    "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
                    "labels_and_ml": {},
                    "robustness": {},
                    "execution_reality": {},
                    "sizing": {},
                    "lineage": {},
                    "acceptance": {},
                },
            }
        ],
        "suppressed_targets": [
            {
                "action": "suppress_symbol",
                "source_task_id": "task-data",
                "source_experiment_id": "exp-data-001",
                "source_outcome": "blocked",
                "market": "options",
                "symbol": "SPY",
            }
        ],
        "planner_actions": [],
    }
    seed_specs = [
        {
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "module_selection": {"features": ["a"], "gates": ["g"]},
            "parameters": {},
            "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
            "labels_and_ml": {},
            "robustness": {},
            "execution_reality": {},
            "sizing": {},
            "lineage": {},
            "acceptance": {},
            "research_intent": "seed",
        },
        {
            "market": "options",
            "strategy_family": "options_scalp",
            "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
            "universe": {"symbols": ["SPY"], "underlying": "SPY"},
            "strategy_ref": {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
            "module_selection": {},
            "parameters": {},
            "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
            "labels_and_ml": {},
            "robustness": {},
            "execution_reality": {},
            "sizing": {},
            "lineage": {},
            "acceptance": {},
            "research_intent": "seed",
        },
    ]
    tasks = compile_agent_tasks(cycle_id="2026-04-05", cycle_policy=cycle_policy, seed_specs=seed_specs)
    strategy_tasks = [t for t in tasks if t.get("task_type") == "strategy_experiment"]
    assert len(strategy_tasks) >= 3  # includes one carry-forward

    lineage_task = next((t for t in strategy_tasks if t.get("research_intent") == "lineage_expand"), None)
    assert lineage_task is not None
    assert lineage_task["derived_from"]["source_task_id"] == "task-pass"

    carry_task = next((t for t in strategy_tasks if t.get("derived_from", {}).get("mapped_action") == "carry_forward"), None)
    assert carry_task is not None
    assert carry_task["derived_from"]["source_task_id"] == "task-carry"

    suppressed_task = next(
        (
            t
            for t in strategy_tasks
            if isinstance(t.get("blocked_by"), dict) and t.get("blocked_by", {}).get("type") == "policy_suppression"
        ),
        None,
    )
    assert suppressed_task is not None
    assert suppressed_task["derived_from"]["source_task_id"] == "task-data"


def test_candidate_pool_and_lineage_are_updated(tmp_path: Path) -> None:
    bundle = {
        "leaderboard": [
            {"experiment_id": "exp-good-001", "result": {"market": "stocks", "strategy_family": "family_a", "lineage": {"family_id": "family_a"}}},
            {"experiment_id": "exp-retest-001", "result": {"market": "options", "strategy_family": "family_b", "lineage": {"family_id": "family_b"}}},
        ],
        "promotion_decision": {
            "winners": [{"experiment_id": "exp-good-001", "tier": "candidate", "score": 1.5}],
            "retest_queue": [{"experiment_id": "exp-retest-001", "reason": "quality_gate_failed", "score": -0.2}],
            "rejected": [],
        },
    }
    artifacts = update_candidate_pool_from_evaluation(
        cycle_id="2026-04-05",
        evaluation_bundle=bundle,
        state_dir=tmp_path / "state",
    )
    pool = json.loads(Path(artifacts["candidate_pool_json"]).read_text(encoding="utf-8"))
    lineage = json.loads(Path(artifacts["lineage_index_json"]).read_text(encoding="utf-8"))
    ids = {row["experiment_id"] for row in pool["candidates"]}
    assert {"exp-good-001", "exp-retest-001"} <= ids
    statuses = {row["experiment_id"]: row["status"] for row in lineage["records"]}
    assert statuses["exp-good-001"] == "promoted"
    assert statuses["exp-retest-001"] == "retest"
