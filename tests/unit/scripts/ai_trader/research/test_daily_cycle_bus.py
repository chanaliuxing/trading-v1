from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from scripts.ai_trader.research.agent import ResearchAgent
from scripts.ai_trader.research.orchestrator import ExperimentOrchestrator
from scripts.ai_trader.research.policy import (
    AutoEvolutionRules,
    ExperimentBudget,
    ResearchPolicy,
    SafetyLimits,
    StrategyFocusRules,
)
from scripts.ai_trader.research.state import BudgetState, Budgets, ResearchState

pytestmark = [
    pytest.mark.filterwarnings("ignore:builtin type SwigPyPacked has no __module__ attribute:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:builtin type SwigPyObject has no __module__ attribute:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:builtin type swigvarlink has no __module__ attribute:DeprecationWarning"),
]


def _minimal_state() -> ResearchState:
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


def _minimal_policy() -> ResearchPolicy:
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
        safety_limits=SafetyLimits(
            max_unreviewed_patches_per_day=1,
            forbidden_targets=[],
        ),
        auto_evolution=AutoEvolutionRules(
            allow_kinds=[],
            require_human_review_for=[],
            max_auto_patches_per_day=0,
            max_files_touched=1,
        ),
    )


def test_research_agent_planning_mode_outputs_structured_experiment_specs() -> None:
    state = _minimal_state()
    policy = _minimal_policy()
    agent = ResearchAgent(llm_client=SimpleNamespace(chat=lambda **_: None))

    async def _fake_call_llm_safe(system: str, user: str, policy_version: str):
        return {
            "focus": [{"strategy": "stocks_scalp", "stage": "validation", "rationale": "test"}],
            "experiments": [
                {
                    "kind": "strategy_experiment",
                    "target": "stocks_scalp",
                    "params": {
                        "market": "stocks",
                        "symbol": "QQQ",
                        "template_id": "qqq_intraday_core",
                    },
                }
            ],
            "meta": {"version": "v1"},
        }

    agent._call_llm_safe = _fake_call_llm_safe  # type: ignore[attr-defined]
    plan = asyncio.run(agent.run_cycle(state, policy, execute_jobs=False))

    assert plan.get("planning_only") is True
    specs = plan.get("experiment_specs")
    assert isinstance(specs, list) and specs
    readiness = plan.get("data_readiness")
    assert isinstance(readiness, dict)
    assert "summary" in readiness
    markets = {str(item.get("market")) for item in specs}
    assert "stocks" in markets
    assert "options" in markets
    assert all(item.get("experiment_id") for item in specs)


def test_orchestrator_execute_experiment_specs_persists_cycle_bundle(monkeypatch, tmp_path: Path) -> None:
    from scripts.ai_trader.research import orchestrator as orch_mod

    async def _fake_execute_job(job):
        spec = dict(job.get("params") or {})
        exp_id = str(spec.get("experiment_id"))
        market = str(spec.get("market") or "")
        return {
            "ok": True,
            "status": "completed",
            "experiment_result": {
                "experiment_id": exp_id,
                "cycle_id": str(spec.get("cycle_id")),
                "status": "completed",
                "market": market,
                "strategy_family": str(spec.get("strategy_family") or f"{market}_family"),
                "summary_metrics": {
                    "trades": 12 if market == "stocks" else 8,
                    "win_rate": 0.62,
                    "net_pnl": 500.0 if market == "stocks" else 350.0,
                    "sharpe": 1.5 if market == "stocks" else 1.2,
                    "max_drawdown_pct": 3.0,
                },
                "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": True},
                "robustness": {
                    "ok": True,
                    "wf_pass": True,
                    "embargo_pass": True,
                    "leakage_score": 0.02,
                    "regime_stability": 0.8,
                    "symbol_transfer_score": 0.7,
                },
                "execution_reality": {"modeled_slippage_bps": 5.0, "fill_ratio": 0.96, "latency_bucket": "sub_2s"},
                "sizing": {},
                "lineage": {},
                "artifacts": {},
                "notes": [],
            },
        }

    monkeypatch.setattr(orch_mod, "RESEARCH_CYCLES_ROOT", tmp_path / "cycles")
    monkeypatch.setattr(orch_mod, "ml_scheduler", SimpleNamespace(execute_job=_fake_execute_job))

    orchestrator = ExperimentOrchestrator()
    cycle_id = "2026-04-05"
    specs = [
        {
            "experiment_id": "exp-stocks-001",
            "cycle_id": cycle_id,
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 20.0},
        },
        {
            "experiment_id": "exp-options-001",
            "cycle_id": cycle_id,
            "market": "options",
            "strategy_family": "options_scalp",
            "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
            "universe": {"underlying": "SPY", "symbols": ["SPY"]},
            "strategy_ref": {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 40.0},
        },
    ]

    result = asyncio.run(
        orchestrator.execute_experiment_specs(
            cycle_id=cycle_id,
            experiment_specs=specs,
            agent_tasks=[],
            agent_plan={"experiments": []},
            state_snapshot={"timestamp": "2026-04-05T00:00:00+00:00"},
            policy_snapshot={"version": "test-v1"},
        )
    )

    artifacts = result["artifacts"]
    cycle_dir = Path(result["cycle_dir"])
    assert cycle_dir.exists()
    assert Path(artifacts["cycle_spec_json"]).exists()
    assert Path(artifacts["task_manifest_json"]).exists()
    assert Path(artifacts["task_execution_log_jsonl"]).exists()
    assert Path(artifacts["task_outcome_index_json"]).exists()
    assert Path(artifacts["task_schedule_plan_json"]).exists()
    assert Path(artifacts["task_budget_summary_json"]).exists()
    assert Path(artifacts["task_retry_report_json"]).exists()
    assert Path(artifacts["cycle_state_json"]).exists()
    assert Path(artifacts["stage_history_jsonl"]).exists()
    assert Path(artifacts["rerun_report_json"]).exists()
    assert Path(artifacts["resume_report_json"]).exists()
    assert Path(artifacts["agent_reports_dir"]).exists()
    assert Path(artifacts["experiments_dir"]).exists()
    assert Path(artifacts["agent_tasks_json"]).exists()
    assert Path(artifacts["agent_tasks_stocks_json"]).exists()
    assert Path(artifacts["agent_tasks_options_json"]).exists()
    assert Path(artifacts["agent_tasks_ml_brain_json"]).exists()
    assert Path(artifacts["agent_tasks_evaluator_json"]).exists()
    compiled_tasks = json.loads(Path(artifacts["agent_tasks_json"]).read_text(encoding="utf-8"))
    assert isinstance(compiled_tasks, list) and compiled_tasks
    first_task = compiled_tasks[0]
    assert "budget_cost" in first_task
    assert "max_runtime_minutes" in first_task
    assert "max_variants" in first_task
    assert "max_retries" in first_task
    assert "retryable" in first_task
    assert "retry_count" in first_task
    assert "failure_policy" in first_task
    assert Path(artifacts["leaderboard_json"]).exists()
    assert Path(artifacts["promotion_decision_json"]).exists()
    assert Path(artifacts["portfolio_evaluation_json"]).exists()
    assert Path(artifacts["blocked_summary_json"]).exists()
    assert Path(artifacts["quality_gate_breakdown_json"]).exists()
    assert Path(artifacts["next_cycle_recommendations_json"]).exists()
    assert Path(artifacts["next_cycle_policy_json"]).exists()
    assert Path(artifacts["feedback_mapping_report_json"]).exists()
    assert Path(artifacts["candidate_pool_json"]).exists()
    assert Path(artifacts["lineage_index_json"]).exists()
    assert Path(artifacts["portfolio_selection_json"]).exists()
    assert Path(artifacts["allocation_plan_json"]).exists()
    assert Path(artifacts["promotion_gate_json"]).exists()
    assert Path(artifacts["shadow_candidates_dir"]).exists()
    assert Path(artifacts["paper_candidates_dir"]).exists()
    assert Path(artifacts["operator_controls_json"]).exists()
    assert Path(artifacts["live_candidates_dir"]).exists()
    assert Path(artifacts["capital_governor_report_json"]).exists()
    assert Path(artifacts["risk_guard_report_json"]).exists()
    assert Path(artifacts["session_control_report_json"]).exists()
    assert Path(artifacts["operator_audit_log_jsonl"]).exists()
    assert Path(artifacts["control_decision_index_json"]).exists()
    assert Path(artifacts["live_runs_dir"]).exists()
    assert Path(artifacts["broker_session_json"]).exists()
    assert Path(artifacts["broker_profile_report_json"]).exists()
    assert Path(artifacts["broker_binding_report_json"]).exists()
    assert Path(artifacts["real_broker_dry_run_json"]).exists()
    assert Path(artifacts["broker_safety_gate_json"]).exists()
    assert Path(artifacts["broker_launch_switch_json"]).exists()
    assert Path(artifacts["broker_readonly_probe_json"]).exists()
    assert Path(artifacts["broker_sandbox_orders_json"]).exists()
    assert Path(artifacts["broker_sandbox_events_jsonl"]).exists()
    assert Path(artifacts["broker_reconcile_report_json"]).exists()
    assert Path(artifacts["eod_closeout_report_json"]).exists()
    assert Path(artifacts["broker_recovery_report_json"]).exists()
    assert Path(artifacts["broker_launch_barrier_json"]).exists()
    assert Path(artifacts["canary_launch_gate_json"]).exists()
    assert Path(artifacts["manual_approval_report_json"]).exists()
    assert Path(artifacts["canary_run_summary_json"]).exists()
    assert Path(artifacts["intraday_monitor_report_json"]).exists()
    assert Path(artifacts["post_trade_attribution_json"]).exists()
    assert Path(artifacts["canary_feedback_report_json"]).exists()
    assert Path(artifacts["live_batch_plan_json"]).exists()
    assert Path(artifacts["live_batch_status_json"]).exists()
    assert Path(artifacts["pre_open_prep_report_json"]).exists()
    assert Path(artifacts["intraday_batch_report_json"]).exists()
    assert Path(artifacts["eod_batch_close_report_json"]).exists()
    assert Path(artifacts["live_ops_summary_json"]).exists()
    assert Path(artifacts["live_batch_feedback_report_json"]).exists()
    assert Path(artifacts["trading_day_status_json"]).exists()
    assert Path(artifacts["ops_control_center_json"]).exists()
    assert Path(artifacts["end_of_day_review_json"]).exists()
    assert Path(artifacts["weekly_candidate_review_json"]).exists()
    assert Path(artifacts["review_plan_patch_json"]).exists()
    assert Path(artifacts["human_ops_summary_json"]).exists()
    assert Path(artifacts["multi_day_run_plan_json"]).exists()
    assert Path(artifacts["multi_day_run_report_json"]).exists()
    assert Path(artifacts["incident_log_jsonl"]).exists()
    assert Path(artifacts["incident_index_json"]).exists()
    assert Path(artifacts["recovery_coordinator_report_json"]).exists()
    assert Path(artifacts["operator_shift_summary_json"]).exists()
    assert Path(artifacts["reliability_metrics_json"]).exists()
    assert Path(artifacts["ops_registry_json"]).exists()
    assert Path(artifacts["orders_state_json"]).exists()
    assert Path(artifacts["orders_events_jsonl"]).exists()
    assert Path(artifacts["account_snapshot_json"]).exists()
    assert Path(artifacts["positions_snapshot_json"]).exists()
    assert Path(artifacts["position_reconcile_report_json"]).exists()
    assert Path(artifacts["live_risk_report_json"]).exists()
    assert Path(artifacts["execution_reconcile_report_json"]).exists()
    assert Path(artifacts["kill_switch_report_json"]).exists()
    assert Path(artifacts["recovery_report_json"]).exists()
    assert Path(artifacts["live_capability_inventory_json"]).exists()
    assert Path(artifacts["live_reuse_plan_json"]).exists()
    assert Path(artifacts["live_integration_report_json"]).exists()
    assert Path(artifacts["live_gap_list_json"]).exists()
    assert Path(artifacts["live_feedback_json"]).exists()
    assert Path(artifacts["shadow_runs_dir"]).exists()
    assert Path(artifacts["execution_feedback_json"]).exists()
    assert Path(artifacts["live_feedback_index_json"]).exists()
    assert Path(artifacts["execution_feedback_report_json"]).exists()
    assert Path(artifacts["candidate_lifecycle_index_json"]).exists()
    assert Path(artifacts["candidate_gate_report_json"]).exists()
    assert Path(artifacts["candidate_retention_report_json"]).exists()
    assert Path(artifacts["candidate_lifecycle_report_json"]).exists()
    assert Path(artifacts["admission_gate_json"]).exists()
    assert Path(artifacts["health_monitor_report_json"]).exists()
    assert Path(artifacts["auto_halt_report_json"]).exists()
    assert Path(artifacts["active_registry_json"]).exists()
    assert Path(artifacts["active_registry_report_json"]).exists()
    assert Path(artifacts["preflight_check_json"]).exists()
    assert Path(artifacts["postflight_check_json"]).exists()
    assert Path(artifacts["acceptance_decision_json"]).exists()
    assert Path(artifacts["artifact_index_json"]).exists()
    assert Path(artifacts["consistency_report_json"]).exists()
    assert Path(artifacts["launch_checklist_json"]).exists()
    assert Path(artifacts["readiness_dashboard_json"]).exists()
    assert Path(artifacts["launch_verdict_json"]).exists()
    assert Path(artifacts["evidence_integrity_report_json"]).exists()
    assert Path(artifacts["launch_batch_json"]).exists()
    assert Path(artifacts["ops_summary_json"]).exists()
    assert Path(artifacts["policy_compatibility_summary_json"]).exists()
    assert Path(artifacts["policy_observability_consumers_json"]).exists()
    assert Path(artifacts["launch_registry_json"]).exists()
    assert Path(artifacts["system_status_json"]).exists()
    assert (Path(artifacts["agent_reports_dir"]) / "data_readiness.json").exists()

    experiment_files = sorted((cycle_dir / "experiments").glob("*.json"))
    assert len(experiment_files) == 2
    first_exp = json.loads(experiment_files[0].read_text(encoding="utf-8"))
    assert first_exp.get("task_id")
    outcome_index = json.loads(Path(artifacts["task_outcome_index_json"]).read_text(encoding="utf-8"))
    assert outcome_index.get("outcomes")
    assert outcome_index["outcomes"][0].get("task_id")
    assert outcome_index["outcomes"][0].get("experiment_id")
    assert "derived_from" in outcome_index["outcomes"][0]

    with Path(artifacts["leaderboard_json"]).open("r", encoding="utf-8") as fh:
        leaderboard = json.load(fh)
    assert isinstance(leaderboard, list) and len(leaderboard) == 2


def test_orchestrator_budget_priority_and_retry_change_execution(monkeypatch, tmp_path: Path) -> None:
    from scripts.ai_trader.research import orchestrator as orch_mod

    calls: list[str] = []
    seen: dict[str, int] = {}

    async def _fake_execute_job(job):
        params = dict(job.get("params") or {})
        task_id = str(params.get("task_id") or "")
        calls.append(task_id)
        seen[task_id] = seen.get(task_id, 0) + 1
        # promote candidate fails first with temporary error, then succeeds after retry
        if params.get("research_intent") == "promote_candidate" and seen[task_id] == 1:
            return {"ok": False, "status": "failed", "error": "temporary_timeout"}
        return {
            "ok": True,
            "status": "completed",
            "experiment_result": {
                "experiment_id": str(params.get("experiment_id")),
                "cycle_id": str(params.get("cycle_id")),
                "status": "completed",
                "market": str(params.get("market") or "stocks"),
                "strategy_family": str(params.get("strategy_family") or "family"),
                "summary_metrics": {"trades": 5, "win_rate": 0.5, "net_pnl": 10.0, "sharpe": 1.0, "max_drawdown_pct": 1.0},
                "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": True},
                "robustness": {
                    "ok": True,
                    "wf_pass": True,
                    "embargo_pass": True,
                    "leakage_score": 0.01,
                    "regime_stability": 0.8,
                    "symbol_transfer_score": 0.7,
                },
                "execution_reality": {},
                "sizing": {},
                "lineage": {},
                "artifacts": {},
                "notes": [],
            },
        }

    monkeypatch.setattr(orch_mod, "RESEARCH_CYCLES_ROOT", tmp_path / "cycles")
    monkeypatch.setattr(orch_mod, "ml_scheduler", SimpleNamespace(execute_job=_fake_execute_job))

    orchestrator = ExperimentOrchestrator()
    cycle_id = "2026-04-06"
    specs = [
        {
            "cycle_id": cycle_id,
            "market": "stocks",
            "strategy_family": "stocks_seed_family",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 20.0},
            "research_intent": "seed",
        },
        {
            "cycle_id": cycle_id,
            "market": "options",
            "strategy_family": "options_promote_family",
            "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
            "universe": {"underlying": "SPY", "symbols": ["SPY"]},
            "strategy_ref": {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 40.0},
            "research_intent": "promote_candidate",
        },
        {
            "cycle_id": cycle_id,
            "market": "stocks",
            "strategy_family": "stocks_retest_data_family",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["IWM"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 20.0},
            "research_intent": "retest_data",
            "blocked_by": {"type": "data_readiness", "reason": "missing_bars", "next_action": "widen_window_or_drop_symbol"},
        },
    ]
    result = asyncio.run(
        orchestrator.execute_experiment_specs(
            cycle_id=cycle_id,
            experiment_specs=specs,
            agent_plan={"task_budget_limits": {"max_cycle_compute_hours": 3.0, "max_cycle_runtime_minutes": 180}},
            state_snapshot={"timestamp": "2026-04-06T00:00:00+00:00"},
            policy_snapshot={"version": "test-v1"},
        )
    )

    artifacts = result["artifacts"]
    schedule = json.loads(Path(artifacts["task_schedule_plan_json"]).read_text(encoding="utf-8"))
    budget = json.loads(Path(artifacts["task_budget_summary_json"]).read_text(encoding="utf-8"))
    retry = json.loads(Path(artifacts["task_retry_report_json"]).read_text(encoding="utf-8"))
    tasks = json.loads(Path(artifacts["agent_tasks_json"]).read_text(encoding="utf-8"))

    assert any(t.get("research_intent") == "promote_candidate" for t in tasks)
    assert budget.get("deferred_count", 0) >= 1  # budget trimming happened
    assert schedule.get("ordered_candidates")
    assert schedule["ordered_candidates"][0] in {t["task_id"] for t in tasks if t.get("research_intent") == "promote_candidate"}
    assert schedule.get("executed_order")
    assert calls[0] in schedule["executed_order"]  # execution follows schedule order
    assert any(item.get("retry_count", 0) >= 1 for item in retry.get("tasks", []))  # temporary failure retried
    assert any(item.get("defer_reason") == "data_readiness_blocked" for item in retry.get("tasks", []))  # blocked task structured defer


def test_cycle_rerun_and_resume_are_idempotent(monkeypatch, tmp_path: Path) -> None:
    from scripts.ai_trader.research import orchestrator as orch_mod

    call_counter = {"total": 0}
    fail_first_call = {"enabled": True}

    async def _fake_execute_job(job):
        params = dict(job.get("params") or {})
        exp_id = str(params.get("experiment_id") or "")
        call_counter["total"] += 1
        if fail_first_call["enabled"]:
            fail_first_call["enabled"] = False
            raise RuntimeError("simulated_execute_crash")
        return {
            "ok": True,
            "status": "completed",
            "experiment_result": {
                "experiment_id": exp_id,
                "cycle_id": str(params.get("cycle_id")),
                "status": "completed",
                "market": str(params.get("market") or "stocks"),
                "strategy_family": str(params.get("strategy_family") or "family"),
                "summary_metrics": {"trades": 6, "win_rate": 0.6, "net_pnl": 120.0, "sharpe": 1.2, "max_drawdown_pct": 2.0},
                "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": True},
                "robustness": {
                    "ok": True,
                    "wf_pass": True,
                    "embargo_pass": True,
                    "leakage_score": 0.01,
                    "regime_stability": 0.8,
                    "symbol_transfer_score": 0.7,
                },
                "execution_reality": {},
                "sizing": {},
                "lineage": {},
                "artifacts": {},
                "notes": [],
            },
        }

    monkeypatch.setattr(orch_mod, "RESEARCH_CYCLES_ROOT", tmp_path / "cycles")
    monkeypatch.setattr(orch_mod, "ml_scheduler", SimpleNamespace(execute_job=_fake_execute_job))
    orchestrator = ExperimentOrchestrator()
    cycle_id = "2026-04-07"
    specs = [
        {
            "experiment_id": "exp-stocks-001",
            "cycle_id": cycle_id,
            "market": "stocks",
            "strategy_family": "stocks_scalp",
            "engine": {"adapter": "stocks_kernel_backtest", "mode": "backtest"},
            "universe": {"symbols": ["QQQ"]},
            "strategy_ref": {"template_id": "qqq_intraday_core"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 20.0},
        },
        {
            "experiment_id": "exp-options-001",
            "cycle_id": cycle_id,
            "market": "options",
            "strategy_family": "options_scalp",
            "engine": {"adapter": "options_single_leg_replay", "mode": "replay"},
            "universe": {"underlying": "SPY", "symbols": ["SPY"]},
            "strategy_ref": {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
            "acceptance": {"min_trades": 1, "max_drawdown_pct": 40.0},
        },
    ]

    with pytest.raises(RuntimeError):
        asyncio.run(
            orchestrator.execute_experiment_specs(
                cycle_id=cycle_id,
                experiment_specs=specs,
                agent_plan={"experiments": []},
                state_snapshot={"timestamp": "2026-04-07T00:00:00+00:00"},
                policy_snapshot={"version": "test-v1"},
            )
        )

    cycle_dir = tmp_path / "cycles" / cycle_id
    state_failed = json.loads((cycle_dir / "cycle_state.json").read_text(encoding="utf-8"))
    assert state_failed.get("status") == "failed"
    assert state_failed.get("failed_stage") == "execute"

    before_resume_calls = call_counter["total"]
    resumed = asyncio.run(
        orchestrator.execute_experiment_specs(
            cycle_id=cycle_id,
            experiment_specs=specs,
            agent_plan={"experiments": []},
            state_snapshot={"timestamp": "2026-04-07T00:00:00+00:00"},
            policy_snapshot={"version": "test-v1"},
            resume=True,
        )
    )
    assert call_counter["total"] > before_resume_calls
    artifacts = resumed["artifacts"]
    state_completed = json.loads(Path(artifacts["cycle_state_json"]).read_text(encoding="utf-8"))
    assert state_completed.get("status") == "completed"

    before_rerun_calls = call_counter["total"]
    rerun_result = asyncio.run(
        orchestrator.execute_experiment_specs(
            cycle_id=cycle_id,
            experiment_specs=specs,
            agent_plan={"experiments": []},
            state_snapshot={"timestamp": "2026-04-07T00:00:00+00:00"},
            policy_snapshot={"version": "test-v1"},
            rerun=True,
        )
    )
    assert call_counter["total"] == before_rerun_calls
    rerun_report = json.loads(Path(rerun_result["artifacts"]["rerun_report_json"]).read_text(encoding="utf-8"))
    assert rerun_report.get("reused_experiments", 0) >= 2
    exp_files = sorted((cycle_dir / "experiments").glob("*.json"))
    assert len(exp_files) == 2
