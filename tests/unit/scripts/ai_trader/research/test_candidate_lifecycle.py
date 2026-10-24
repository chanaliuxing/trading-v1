from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.agent_tasking import compile_agent_tasks
from scripts.ai_trader.research.candidate_gate import build_candidate_gate
from scripts.ai_trader.research.candidate_lifecycle import (
    apply_candidate_lifecycle,
    load_candidate_lifecycle_index,
    persist_candidate_lifecycle_index,
)
from scripts.ai_trader.research.candidate_retention import build_candidate_retention
from scripts.ai_trader.research.cycle_policy import build_next_cycle_policy


def test_candidate_lifecycle_promote_demote_retire_and_stale(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    base_index = {
        "updated_at": None,
        "candidates": [
            {
                "candidate_id": "cand-demote",
                "experiment_id": "exp-demote",
                "current_state": "paper_active",
                "state_entered_at": "2026-04-01T00:00:00+00:00",
                "stale_cycles": 0,
                "retention": {"cycles_observed": 2, "demotion_count": 0},
            },
            {
                "candidate_id": "cand-retire",
                "experiment_id": "exp-retire",
                "current_state": "demoted",
                "state_entered_at": "2026-04-01T00:00:00+00:00",
                "stale_cycles": 0,
                "retention": {"cycles_observed": 2, "demotion_count": 2},
            },
            {
                "candidate_id": "cand-stale",
                "experiment_id": "exp-stale",
                "current_state": "shadow_candidate",
                "state_entered_at": "2026-04-01T00:00:00+00:00",
                "stale_cycles": 0,
                "retention": {"cycles_observed": 1, "demotion_count": 0},
            },
        ],
    }
    persist_candidate_lifecycle_index(index=base_index, state_root=state_root)
    loaded = load_candidate_lifecycle_index(state_root=state_root)
    execution_feedback = {
        "items": [
            {
                "candidate_id": "cand-promote",
                "experiment_id": "exp-promote",
                "feedback_status": "execution_ok",
                "market": "stocks",
                "symbol": "QQQ",
                "strategy_family": "stocks_scalp",
                "slippage_delta": 0.1,
                "fill_quality": 0.02,
                "run_mode": "shadow",
            },
            {
                "candidate_id": "cand-demote",
                "experiment_id": "exp-demote",
                "feedback_status": "slippage_bad",
                "market": "options",
                "symbol": "SPY",
                "strategy_family": "options_scalp",
                "slippage_delta": 3.2,
                "fill_quality": -0.06,
                "run_mode": "paper",
            },
            {
                "candidate_id": "cand-retire",
                "experiment_id": "exp-retire",
                "feedback_status": "fill_quality_bad",
                "market": "stocks",
                "symbol": "IWM",
                "strategy_family": "stocks_scalp",
                "slippage_delta": 2.5,
                "fill_quality": -0.1,
                "run_mode": "shadow",
            },
        ]
    }
    promotion_gate = {
        "items": [
            {"experiment_id": "cand-promote", "gate_status": "passed", "promotion_target": "paper_candidate"},
            {"experiment_id": "cand-demote", "gate_status": "passed", "promotion_target": "paper_candidate"},
            {"experiment_id": "cand-retire", "gate_status": "rejected", "promotion_target": "research_only"},
        ]
    }
    retention = build_candidate_retention(
        cycle_id="2026-04-05",
        execution_feedback=execution_feedback,
        lifecycle_index=loaded,
    )
    gate = build_candidate_gate(
        cycle_id="2026-04-05",
        lifecycle_index=loaded,
        promotion_gate=promotion_gate,
        execution_feedback=execution_feedback,
        retention_report=retention,
        min_cycles_for_promote=1,
    )
    stale_first = next(x for x in gate["items"] if x["candidate_id"] == "cand-stale")
    assert stale_first["gate_action"] == "stale"
    first_apply = apply_candidate_lifecycle(
        cycle_id="2026-04-05",
        lifecycle_index=loaded,
        candidate_gate_report=gate,
        candidate_retention_report=retention,
    )
    rows = {x["candidate_id"]: x for x in first_apply["index"]["candidates"]}
    assert rows["cand-promote"]["current_state"] in {"paper_candidate", "paper_active", "shadow_active"}
    assert rows["cand-demote"]["current_state"] == "demoted"
    assert rows["cand-retire"]["current_state"] == "retired"

    # no recent evidence for cand-stale on next cycle -> stale first, then retire
    second_retention = build_candidate_retention(
        cycle_id="2026-04-06",
        execution_feedback={"items": []},
        lifecycle_index=first_apply["index"],
    )
    second_gate = build_candidate_gate(
        cycle_id="2026-04-06",
        lifecycle_index=first_apply["index"],
        promotion_gate={"items": []},
        execution_feedback={"items": []},
        retention_report=second_retention,
        min_cycles_for_promote=1,
    )
    second_apply = apply_candidate_lifecycle(
        cycle_id="2026-04-06",
        lifecycle_index=first_apply["index"],
        candidate_gate_report=second_gate,
        candidate_retention_report=second_retention,
    )
    stale_row = next(x for x in second_gate["items"] if x["candidate_id"] == "cand-stale")
    assert stale_row["gate_action"] == "retire"
    third_gate = build_candidate_gate(
        cycle_id="2026-04-07",
        lifecycle_index=second_apply["index"],
        promotion_gate={"items": []},
        execution_feedback={"items": []},
        retention_report=second_retention,
        min_cycles_for_promote=1,
    )
    third_apply = apply_candidate_lifecycle(
        cycle_id="2026-04-07",
        lifecycle_index=second_apply["index"],
        candidate_gate_report=third_gate,
        candidate_retention_report=second_retention,
    )
    rows3 = {x["candidate_id"]: x for x in third_apply["index"]["candidates"]}
    assert rows3["cand-stale"]["current_state"] == "retired"


def test_policy_consumes_lifecycle_and_task_compiler_skips_retired() -> None:
    bundle = {
        "leaderboard": [],
        "blocked_summary": {"by_market": {"options": 0, "stocks": 0}},
        "next_cycle_recommendations": {"actions": []},
        "promotion_decision": {"winners": [], "blocked_by_data": [], "blocked_by_robustness": []},
    }
    lifecycle = {
        "candidates": [
            {
                "candidate_id": "cand-r",
                "experiment_id": "exp-r",
                "current_state": "retired",
                "market": "stocks",
                "symbol": "QQQ",
                "strategy_family": "stocks_scalp",
            },
            {
                "candidate_id": "cand-d",
                "experiment_id": "exp-d",
                "current_state": "demoted",
                "market": "options",
                "symbol": "SPY",
                "strategy_family": "options_scalp",
            },
        ]
    }
    policy = build_next_cycle_policy(
        bundle,
        cycle_id="2026-04-05",
        task_outcome_index={"outcomes": []},
        task_schedule_plan={"entries": []},
        task_retry_report={"tasks": []},
        task_manifest={"tasks": []},
        execution_feedback={"items": []},
        candidate_lifecycle_index=lifecycle,
    )
    assert any(x.get("action") == "suppress_retired_candidate" for x in policy["suppressed_targets"])
    assert any(x.get("action") == "demoted_retest_only" for x in policy["task_patches"])

    seed_specs = [
        {
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
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
    tasks = compile_agent_tasks(cycle_id="2026-04-06", cycle_policy=policy, seed_specs=seed_specs)
    assert not any(t["agent_role"] == "stocks_scalp" for t in tasks if t["task_type"] == "strategy_experiment")
    options_task = next(t for t in tasks if t["agent_role"] == "options_scalp" and t["task_type"] == "strategy_experiment")
    assert options_task["research_intent"] == "retest_robustness"
    json.dumps(tasks, ensure_ascii=False)
