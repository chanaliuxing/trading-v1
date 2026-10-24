from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.cycle_policy import build_next_cycle_policy
from scripts.ai_trader.research.execution_feedback import build_execution_feedback
from scripts.ai_trader.research.live_bridge import build_live_candidates
from scripts.ai_trader.research.live_executor import run_live_executor
from scripts.ai_trader.research.live_feedback import build_live_feedback, persist_live_feedback
from scripts.ai_trader.research.live_risk_guard import evaluate_live_risk_guard
from scripts.ai_trader.research.paper_bridge import build_paper_candidates
from scripts.ai_trader.research.shadow_bundle import build_shadow_candidates
from scripts.ai_trader.research.shadow_runner import run_shadow_candidates


def test_live_bridge_risk_guard_executor_and_feedback(tmp_path: Path) -> None:
    cycle_dir = tmp_path / "cycle"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    promotion_gate = {
        "items": [
            {
                "experiment_id": "exp-live-001",
                "gate_status": "passed",
                "promotion_target": "paper_candidate",
                "gate_reasons": ["passed_all_gates"],
            },
            {
                "experiment_id": "exp-live-002",
                "gate_status": "passed",
                "promotion_target": "shadow_candidate",
                "gate_reasons": ["passed_all_gates"],
            },
        ]
    }
    allocation_plan = {
        "items": [
            {
                "experiment_id": "exp-live-001",
                "target_weight": 0.7,
                "max_capital": 70_000,
                "risk_budget_bps": 30,
                "allocation_reason": "test",
            },
            {
                "experiment_id": "exp-live-002",
                "target_weight": 0.3,
                "max_capital": 30_000,
                "risk_budget_bps": 15,
                "allocation_reason": "test",
            },
        ]
    }
    candidate_results = [
        {"experiment_id": "exp-live-001", "market": "stocks", "universe": {"symbols": ["QQQ"]}, "strategy_family": "stocks_scalp"},
        {"experiment_id": "exp-live-002", "market": "options", "universe": {"symbols": ["SPY"]}, "strategy_family": "options_scalp"},
    ]

    shadow = build_shadow_candidates(
        promotion_gate=promotion_gate,
        allocation_plan=allocation_plan,
        candidate_results=candidate_results,
        output_dir=cycle_dir,
    )
    paper = build_paper_candidates(shadow_candidates_dir=shadow["shadow_candidates_dir"], output_dir=cycle_dir)
    live = build_live_candidates(paper_candidates_dir=paper["paper_candidates_dir"], output_dir=cycle_dir)
    assert live["live_manifests"]
    risk = evaluate_live_risk_guard(
        cycle_id="2026-04-05",
        live_candidates_dir=live["live_candidates_dir"],
        simulate_kill_switch_candidate="exp-live-001",
    )
    assert any(item.get("kill_switch") for item in risk["items"])
    (cycle_dir / "risk_guard_report.json").write_text(json.dumps(risk, indent=2, ensure_ascii=False), encoding="utf-8")

    live_run = run_live_executor(
        cycle_id="2026-04-05",
        live_candidates_dir=live["live_candidates_dir"],
        risk_guard_report=risk,
        output_dir=cycle_dir,
    )
    assert live_run["items"]
    assert (cycle_dir / "live_runs" / "exp-live-002" / "execution_summary.json").exists()
    shadow_runs = run_shadow_candidates(shadow_candidates_dir=shadow["shadow_candidates_dir"], output_dir=cycle_dir)
    shadow_feedback = build_execution_feedback(cycle_id="2026-04-05", shadow_runs_dir=shadow_runs["shadow_runs_dir"])
    live_feedback = build_live_feedback(cycle_id="2026-04-05", live_execution_report=live_run)
    persisted = persist_live_feedback(feedback=live_feedback, output_dir=cycle_dir, state_root=tmp_path / "state")
    assert Path(persisted["live_feedback_json"]).exists()
    assert Path(persisted["live_feedback_index_json"]).exists()

    combined_feedback = {
        "items": list(shadow_feedback.get("items") or []) + list(live_feedback.get("policy_mapped_feedback") or [])
    }
    policy = build_next_cycle_policy(
        {
            "leaderboard": [],
            "blocked_summary": {"by_market": {"options": 0, "stocks": 0}},
            "next_cycle_recommendations": {"actions": []},
            "promotion_decision": {"winners": [], "blocked_by_data": [], "blocked_by_robustness": []},
        },
        cycle_id="2026-04-05",
        task_outcome_index={"outcomes": []},
        task_schedule_plan={"entries": []},
        task_retry_report={"tasks": []},
        task_manifest={"tasks": []},
        execution_feedback=combined_feedback,
        candidate_lifecycle_index={"candidates": []},
    )
    assert any(x.get("action") == "tighten_execution_constraints" for x in policy["task_patches"])
