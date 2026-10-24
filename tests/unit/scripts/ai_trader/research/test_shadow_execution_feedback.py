from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.cycle_policy import build_next_cycle_policy
from scripts.ai_trader.research.execution_feedback import build_execution_feedback, persist_execution_feedback
from scripts.ai_trader.research.paper_bridge import build_paper_candidates
from scripts.ai_trader.research.shadow_bundle import build_shadow_candidates
from scripts.ai_trader.research.shadow_runner import run_shadow_candidates


def test_shadow_runner_and_feedback_artifacts(tmp_path: Path) -> None:
    cycle_dir = tmp_path / "cycle"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    promotion_gate = {
        "items": [
            {
                "experiment_id": "exp-shadow-001",
                "gate_status": "passed",
                "promotion_target": "shadow_candidate",
                "gate_reasons": ["passed_all_gates"],
            }
        ]
    }
    allocation_plan = {
        "items": [
            {
                "experiment_id": "exp-shadow-001",
                "target_weight": 0.6,
                "max_capital": 60_000,
                "risk_budget_bps": 60,
                "allocation_reason": "test",
            }
        ]
    }
    candidate_results = [
        {
            "experiment_id": "exp-shadow-001",
            "task_id": "task-shadow-001",
            "status": "completed",
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "universe": {"symbols": ["QQQ"]},
        }
    ]

    shadow = build_shadow_candidates(
        promotion_gate=promotion_gate,
        allocation_plan=allocation_plan,
        candidate_results=candidate_results,
        output_dir=cycle_dir,
    )
    paper = build_paper_candidates(
        shadow_candidates_dir=shadow["shadow_candidates_dir"],
        output_dir=cycle_dir,
    )
    runs = run_shadow_candidates(
        shadow_candidates_dir=shadow["shadow_candidates_dir"],
        output_dir=cycle_dir,
    )
    feedback = build_execution_feedback(cycle_id="2026-04-05", shadow_runs_dir=runs["shadow_runs_dir"])
    artifacts = persist_execution_feedback(feedback=feedback, output_dir=cycle_dir, state_root=tmp_path / "state")

    candidate_manifest = Path(shadow["candidate_dirs"][0]) / "candidate_manifest.json"
    run_summary = Path(runs["shadow_runs_dir"]) / "exp-shadow-001" / "run_summary.json"
    paper_manifest = Path(paper["paper_candidates_dir"]) / "exp-shadow-001" / "paper_manifest.json"
    assert candidate_manifest.exists()
    assert run_summary.exists()
    assert paper_manifest.exists()
    assert Path(artifacts["execution_feedback_json"]).exists()
    assert Path(artifacts["live_feedback_index_json"]).exists()


def test_execution_feedback_maps_to_next_cycle_policy_actions() -> None:
    bundle = {
        "leaderboard": [],
        "blocked_summary": {"by_market": {"options": 0, "stocks": 0}},
        "next_cycle_recommendations": {"actions": []},
        "promotion_decision": {
            "winners": [],
            "blocked_by_data": [],
            "blocked_by_robustness": [],
        },
    }
    execution_feedback = {
        "items": [
            {
                "candidate_id": "cand-1",
                "experiment_id": "exp-a",
                "source_task_id": "task-a",
                "market": "stocks",
                "symbol": "QQQ",
                "strategy_family": "stocks_scalp",
                "feedback_status": "slippage_bad",
                "next_action": "tighten_execution_constraints",
            }
        ]
    }
    policy = build_next_cycle_policy(
        bundle,
        cycle_id="2026-04-05",
        task_outcome_index={"outcomes": [{"task_id": "task-a", "experiment_id": "exp-a", "status": "completed"}]},
        task_schedule_plan={"entries": []},
        task_retry_report={"tasks": []},
        task_manifest={"tasks": [{"task_id": "task-a", "task_patch": {"market": "stocks"}}]},
        execution_feedback=execution_feedback,
    )
    actions = [x for x in policy["task_patches"] if x.get("action") == "tighten_execution_constraints"]
    assert actions
    assert policy["execution_feedback"]["items"][0]["feedback_status"] == "slippage_bad"
    report = policy["feedback_mapping_report"]
    assert report["summary"]["execution_actions"] >= 1
    # keep lint/flake quiet by proving json-serializable policy payload
    json.dumps(policy, ensure_ascii=False)
