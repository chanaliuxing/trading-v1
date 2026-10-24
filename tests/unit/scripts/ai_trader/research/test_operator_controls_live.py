from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.capital_governor import evaluate_capital_governor
from scripts.ai_trader.research.cycle_policy import build_next_cycle_policy
from scripts.ai_trader.research.live_bridge import build_live_candidates
from scripts.ai_trader.research.live_executor import run_live_executor
from scripts.ai_trader.research.live_feedback import build_live_feedback
from scripts.ai_trader.research.live_risk_guard import evaluate_live_risk_guard
from scripts.ai_trader.research.operator_audit import persist_operator_audit
from scripts.ai_trader.research.operator_controls import persist_operator_controls
from scripts.ai_trader.research.paper_bridge import build_paper_candidates
from scripts.ai_trader.research.session_control import evaluate_session_control
from scripts.ai_trader.research.shadow_bundle import build_shadow_candidates


def _setup_live_candidates(tmp_path: Path) -> tuple[str, str]:
    cycle_dir = tmp_path / "cycle"
    promotion_gate = {
        "items": [
            {"experiment_id": "exp-op-1", "gate_status": "passed", "promotion_target": "paper_candidate", "gate_reasons": ["ok"]},
            {"experiment_id": "exp-op-2", "gate_status": "passed", "promotion_target": "paper_candidate", "gate_reasons": ["ok"]},
        ]
    }
    allocation_plan = {
        "items": [
            {"experiment_id": "exp-op-1", "target_weight": 0.75, "max_capital": 75_000, "risk_budget_bps": 40},
            {"experiment_id": "exp-op-2", "target_weight": 0.35, "max_capital": 35_000, "risk_budget_bps": 20},
        ]
    }
    candidate_results = [
        {"experiment_id": "exp-op-1", "market": "stocks", "universe": {"symbols": ["QQQ"]}, "strategy_family": "stocks_scalp"},
        {"experiment_id": "exp-op-2", "market": "stocks", "universe": {"symbols": ["IWM"]}, "strategy_family": "stocks_scalp"},
    ]
    shadow = build_shadow_candidates(
        promotion_gate=promotion_gate,
        allocation_plan=allocation_plan,
        candidate_results=candidate_results,
        output_dir=cycle_dir,
    )
    paper = build_paper_candidates(shadow_candidates_dir=shadow["shadow_candidates_dir"], output_dir=cycle_dir)
    return str(cycle_dir), paper["paper_candidates_dir"]


def test_operator_controls_capital_session_and_audit(tmp_path: Path) -> None:
    cycle_dir, paper_candidates_dir = _setup_live_candidates(tmp_path)
    state_root = tmp_path / "state"
    controls_path = persist_operator_controls(
        {
            "global_pause": True,
            "paper_only": True,
            "live_enabled": True,
            "max_live_capital": 60_000.0,
            "blocked_candidates": ["exp-op-2"],
            "blocked_symbols": ["IWM"],
            "force_flatten": True,
            "cooldown_candidates": ["exp-op-1"],
            "no_trade_windows": ["00:00-23:59"],
            "notes": "unit-test-controls",
        },
        state_root=state_root,
    )
    controls = json.loads(Path(controls_path).read_text(encoding="utf-8"))
    live = build_live_candidates(
        paper_candidates_dir=paper_candidates_dir,
        output_dir=cycle_dir,
        operator_state_root=state_root,
    )
    cap = evaluate_capital_governor(
        cycle_id="2026-04-05",
        live_candidates_dir=live["live_candidates_dir"],
        operator_controls=controls,
        live_feedback_index={},
    )
    assert any(x["decision"] == "downscaled" for x in cap["items"])
    assert any(x["decision"] == "rejected" for x in cap["items"])

    risk = evaluate_live_risk_guard(
        cycle_id="2026-04-05",
        live_candidates_dir=live["live_candidates_dir"],
        operator_state_root=state_root,
    )
    sess = evaluate_session_control(
        cycle_id="2026-04-05",
        live_candidates_dir=live["live_candidates_dir"],
        operator_controls=controls,
        risk_guard_report=risk,
        simulate_pre_close_flatten=True,
    )
    assert any("global_pause" in x["reasons"] for x in sess["items"])
    assert any("blocked_candidate" in x["reasons"] for x in sess["items"])
    assert any("no_trade_window" in x["reasons"] or "cooldown_after_kill_switch" in x["reasons"] for x in sess["items"])
    assert any(x["flatten_required"] for x in sess["items"])

    runs = run_live_executor(
        cycle_id="2026-04-05",
        live_candidates_dir=live["live_candidates_dir"],
        risk_guard_report=risk,
        capital_governor_report=cap,
        session_control_report=sess,
        output_dir=cycle_dir,
        operator_state_root=state_root,
    )
    assert runs["items"]

    live_feedback = build_live_feedback(cycle_id="2026-04-05", live_execution_report=runs)
    policy = build_next_cycle_policy(
        {
            "leaderboard": [],
            "blocked_summary": {"by_market": {"stocks": 0, "options": 0}},
            "next_cycle_recommendations": {"actions": []},
            "promotion_decision": {"winners": [], "blocked_by_data": [], "blocked_by_robustness": []},
        },
        cycle_id="2026-04-05",
        task_outcome_index={"outcomes": []},
        task_schedule_plan={"entries": []},
        task_retry_report={"tasks": []},
        task_manifest={"tasks": []},
        execution_feedback={"items": live_feedback["policy_mapped_feedback"]},
        candidate_lifecycle_index={"candidates": []},
    )
    assert any(x.get("source_outcome") in {"fill_quality_bad", "slippage_bad"} for x in policy["task_patches"])

    audit = persist_operator_audit(
        cycle_id="2026-04-05",
        output_dir=cycle_dir,
        operator_controls=controls,
        capital_governor_report=cap,
        session_control_report=sess,
    )
    assert Path(audit["operator_audit_log_jsonl"]).exists()
    idx = json.loads(Path(audit["control_decision_index_json"]).read_text(encoding="utf-8"))
    assert idx["samples"]["capital_reject"] is not None
    assert idx["samples"]["pause"] is not None
    assert idx["samples"]["flatten"] is not None
