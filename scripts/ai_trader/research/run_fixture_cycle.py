from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.agent_tasking import compile_agent_tasks, persist_agent_tasks
from scripts.ai_trader.research.candidate_gate import build_candidate_gate
from scripts.ai_trader.research.candidate_lifecycle import (
    apply_candidate_lifecycle,
    load_candidate_lifecycle_index,
    persist_candidate_lifecycle_index,
)
from scripts.ai_trader.research.candidate_retention import build_candidate_retention
from scripts.ai_trader.research.admission_gate import build_admission_gate
from scripts.ai_trader.research.health_monitor import build_health_monitor_report
from scripts.ai_trader.research.auto_halt import build_auto_halt_report
from scripts.ai_trader.research.active_registry import load_active_registry, persist_active_registry, update_active_registry
from scripts.ai_trader.research.preflight_check import build_preflight_check
from scripts.ai_trader.research.launch_checklist import build_launch_checklist
from scripts.ai_trader.research.readiness_dashboard import build_readiness_dashboard
from scripts.ai_trader.research.launch_verdict import build_launch_verdict
from scripts.ai_trader.research.evidence_integrity import build_evidence_integrity_report
from scripts.ai_trader.research.launch_batch import build_launch_batch
from scripts.ai_trader.research.ops_summary import (
    build_ops_summary,
    persist_launch_registry,
    persist_system_status,
)
from scripts.ai_trader.research.policy_compatibility_summary import persist_policy_compatibility_summary
from scripts.ai_trader.research.policy_observability_consumers import persist_policy_observability_consumer_inventory
from scripts.ai_trader.research.candidate_pool import update_candidate_pool_from_evaluation
from scripts.ai_trader.research.cycle_policy import persist_next_cycle_policy
from scripts.ai_trader.research.execution_feedback import build_execution_feedback, persist_execution_feedback
from scripts.ai_trader.research.evaluator import persist_evaluation_bundle
from scripts.ai_trader.research.capital_governor import evaluate_capital_governor
from scripts.ai_trader.research.live_bridge import build_live_candidates
from scripts.ai_trader.research.live_executor import run_live_executor
from scripts.ai_trader.research.live_feedback import build_live_feedback, persist_live_feedback
from scripts.ai_trader.research.live_risk_guard import evaluate_live_risk_guard
from scripts.ai_trader.research.order_manager import run_order_manager
from scripts.ai_trader.research.account_sync import sync_account_snapshot
from scripts.ai_trader.research.position_sync import sync_positions_snapshot
from scripts.ai_trader.research.live_risk_enforcer import enforce_live_risk
from scripts.ai_trader.research.execution_reconcile import run_execution_reconcile
from scripts.ai_trader.research.live_kill_switch import run_live_kill_switch
from scripts.ai_trader.research.recovery_playbook import run_recovery_playbook
from scripts.ai_trader.research.broker_safety_gate import build_broker_safety_gate
from scripts.ai_trader.research.broker_readonly_probe import run_broker_readonly_probe
from scripts.ai_trader.research.broker_sandbox_orders import run_broker_sandbox_orders
from scripts.ai_trader.research.broker_reconcile import run_broker_reconcile
from scripts.ai_trader.research.eod_closeout import run_eod_closeout
from scripts.ai_trader.research.broker_recovery import run_broker_recovery
from scripts.ai_trader.research.broker_launch_barrier import apply_broker_launch_barrier
from scripts.ai_trader.research.manual_approval import persist_manual_approval, build_manual_approval_report
from scripts.ai_trader.research.canary_launch_gate import build_canary_launch_gate
from scripts.ai_trader.research.canary_executor import run_canary_executor
from scripts.ai_trader.research.intraday_monitor import build_intraday_monitor_report
from scripts.ai_trader.research.post_trade_attribution import build_post_trade_attribution
from scripts.ai_trader.research.canary_feedback import apply_canary_feedback
from scripts.ai_trader.research.live_batch_orchestrator import build_live_batch_plan
from scripts.ai_trader.research.pre_open_prep import run_pre_open_prep
from scripts.ai_trader.research.intraday_batch_supervisor import run_intraday_batch_supervisor
from scripts.ai_trader.research.eod_batch_close import run_eod_batch_close
from scripts.ai_trader.research.live_ops_summary import build_live_ops_summary
from scripts.ai_trader.research.live_batch_feedback import apply_live_batch_feedback
from scripts.ai_trader.research.trading_day_controller import build_trading_day_status
from scripts.ai_trader.research.ops_control_center import build_ops_control_center
from scripts.ai_trader.research.end_of_day_review import build_end_of_day_review
from scripts.ai_trader.research.weekly_candidate_review import build_weekly_candidate_review
from scripts.ai_trader.research.review_to_plan_bridge import build_review_plan_patch
from scripts.ai_trader.research.human_ops_summary import build_human_ops_summary
from scripts.ai_trader.research.multi_day_runner import build_multi_day_run_artifacts
from scripts.ai_trader.research.incident_manager import build_incident_artifacts
from scripts.ai_trader.research.recovery_coordinator import build_recovery_coordinator_report
from scripts.ai_trader.research.operator_shift_summary import build_operator_shift_summary
from scripts.ai_trader.research.reliability_metrics import build_reliability_metrics
from scripts.ai_trader.research.ops_registry import update_ops_registry
from scripts.ai_trader.research.ops_hardening_acceptance import (
    build_acceptance_decision,
    build_artifact_index,
    build_consistency_report,
    build_postflight_check,
    write_json_crash_safe,
)
from scripts.ai_trader.research.live_reuse_audit import generate_live_reuse_artifacts
from scripts.ai_trader.research.operator_audit import persist_operator_audit
from scripts.ai_trader.research.operator_controls import persist_operator_controls
from scripts.ai_trader.research.paper_bridge import build_paper_candidates
from scripts.ai_trader.research.portfolio_selection import build_portfolio_selection, persist_portfolio_selection
from scripts.ai_trader.research.promotion_gate import build_promotion_gate, persist_promotion_gate
from scripts.ai_trader.research.shadow_bundle import build_allocation_plan, build_shadow_candidates, persist_allocation_plan
from scripts.ai_trader.research.shadow_runner import run_shadow_candidates
from scripts.ai_trader.research.session_control import evaluate_session_control


def _load_json_safe(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _load_first_json(paths: list[Path]) -> dict:
    for path in paths:
        payload = _load_json_safe(path)
        if payload:
            return payload
    return {}


def _load_preflight_policy_context(*, state_root: Path, cycle_dir: Path) -> dict:
    preflight_policy = _load_first_json(
        [
            cycle_dir / "preflight-policy.json",
            state_root / "preflight-policy.json",
            cycle_dir / "preflight_policy.json",
            state_root / "preflight_policy.json",
        ]
    )
    preflight_history = _load_first_json(
        [
            cycle_dir / "preflight_release_history.json",
            state_root / "preflight_release_history.json",
        ]
    )
    return {"preflight_policy": preflight_policy, "preflight_history": preflight_history}


def _fixture_results(cycle_id: str) -> list[dict]:
    return [
        {
            "experiment_id": f"{cycle_id}-stocks-passed",
            "cycle_id": cycle_id,
            "status": "completed",
            "market": "stocks",
            "strategy_family": "stocks_fixture",
            "summary_metrics": {
                "trades": 24,
                "win_rate": 0.61,
                "net_pnl": 1320.0,
                "sharpe": 1.7,
                "max_drawdown_pct": 2.8,
            },
            "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": True},
            "robustness": {
                "ok": True,
                "status": "ok",
                "wf_pass": True,
                "embargo_pass": True,
                "leakage_score": 0.01,
                "regime_stability": 0.84,
                "symbol_transfer_score": 0.71,
                "passed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
                "failed_checks": [],
                "failure_type": None,
                "next_action": None,
            },
            "execution_reality": {
                "modeled_slippage_bps": 5.0,
                "spread_cost_bps": 2.0,
                "partial_fill_rate": 0.05,
                "latency_bucket": "sub_2s",
            },
            "notes": [],
        },
        {
            "experiment_id": f"{cycle_id}-options-blocked-data",
            "cycle_id": cycle_id,
            "status": "blocked",
            "market": "options",
            "strategy_family": "options_fixture",
            "summary_metrics": {},
            "quality_flags": {"data_ok": False, "enough_trades": False, "passed_acceptance": False},
            "robustness": {
                "ok": False,
                "status": "fail",
                "wf_pass": False,
                "embargo_pass": False,
                "leakage_score": 1.0,
                "regime_stability": 0.0,
                "symbol_transfer_score": 0.0,
                "passed_checks": [],
                "failed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
                "failure_type": "data_insufficient",
                "next_action": "widen_window_or_drop_symbol",
                "issues": ["data_readiness_blocked"],
            },
            "blocked_by": {
                "type": "data_readiness",
                "reason": "no_bars_found",
                "market": "options",
                "symbol": "SPY",
                "window": {"start": "2026-03-01", "end": "2026-03-31"},
                "next_action": "widen_window_or_drop_symbol",
            },
            "notes": ["data_readiness_blocked:no_bars_found"],
        },
        {
            "experiment_id": f"{cycle_id}-stocks-robustness-fail",
            "cycle_id": cycle_id,
            "status": "completed",
            "market": "stocks",
            "strategy_family": "stocks_fixture",
            "summary_metrics": {
                "trades": 18,
                "win_rate": 0.58,
                "net_pnl": 420.0,
                "sharpe": 0.9,
                "max_drawdown_pct": 4.1,
            },
            "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": False},
            "robustness": {
                "ok": False,
                "status": "fail",
                "wf_pass": False,
                "embargo_pass": True,
                "leakage_score": 0.12,
                "regime_stability": 0.42,
                "symbol_transfer_score": 0.31,
                "passed_checks": ["embargo"],
                "failed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
                "failure_type": "threshold_not_met",
                "next_action": "increase_folds_or_window_expand",
            },
            "execution_reality": {
                "modeled_slippage_bps": 6.5,
                "spread_cost_bps": 2.5,
                "partial_fill_rate": 0.12,
                "latency_bucket": "sub_2s",
            },
            "notes": ["robustness_threshold_not_met"],
        },
        {
            "experiment_id": f"{cycle_id}-stocks-passed-2",
            "cycle_id": cycle_id,
            "status": "completed",
            "market": "stocks",
            "strategy_family": "stocks_fixture_alt",
            "summary_metrics": {
                "trades": 20,
                "win_rate": 0.6,
                "net_pnl": 980.0,
                "sharpe": 1.45,
                "max_drawdown_pct": 3.1,
            },
            "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": True},
            "robustness": {
                "ok": True,
                "status": "ok",
                "wf_pass": True,
                "embargo_pass": True,
                "leakage_score": 0.02,
                "regime_stability": 0.81,
                "symbol_transfer_score": 0.68,
                "passed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
                "failed_checks": [],
                "failure_type": None,
                "next_action": None,
            },
            "execution_reality": {
                "modeled_slippage_bps": 5.5,
                "spread_cost_bps": 2.1,
                "partial_fill_rate": 0.06,
                "latency_bucket": "sub_2s",
            },
            "notes": [],
        },
        {
            "experiment_id": f"{cycle_id}-options-passed",
            "cycle_id": cycle_id,
            "status": "completed",
            "market": "options",
            "strategy_family": "options_fixture_alt",
            "summary_metrics": {
                "trades": 16,
                "win_rate": 0.59,
                "net_pnl": 760.0,
                "sharpe": 1.2,
                "max_drawdown_pct": 4.3,
            },
            "quality_flags": {"data_ok": True, "enough_trades": True, "passed_acceptance": True},
            "robustness": {
                "ok": True,
                "status": "ok",
                "wf_pass": True,
                "embargo_pass": True,
                "leakage_score": 0.03,
                "regime_stability": 0.76,
                "symbol_transfer_score": 0.64,
                "passed_checks": ["walk_forward", "leakage", "regime_split", "cross_symbol"],
                "failed_checks": [],
                "failure_type": None,
                "next_action": None,
            },
            "execution_reality": {
                "modeled_slippage_bps": 7.0,
                "spread_cost_bps": 3.8,
                "partial_fill_rate": 0.08,
                "latency_bucket": "sub_2s",
            },
            "notes": [],
        },
    ]


def _seed_fixture_lifecycle_controls(index: dict, *, cycle_id: str) -> dict:
    payload = dict(index or {})
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        rows = []
    by_id = {str(x.get("candidate_id") or ""): dict(x) for x in rows if isinstance(x, dict) and str(x.get("candidate_id") or "")}
    controls = [
        {
            "candidate_id": f"{cycle_id}-stocks-passed",
            "experiment_id": f"{cycle_id}-stocks-passed",
            "current_state": "research_only",
            "state_entered_at": "2026-04-01T00:00:00+00:00",
            "last_evaluated_at": "2026-04-04T00:00:00+00:00",
            "stale_cycles": 0,
            "market": "stocks",
            "symbol": "QQQ",
            "strategy_family": "stocks_fixture",
            "retention": {"cycles_observed": 1, "demotion_count": 0},
        },
        {
            "candidate_id": f"{cycle_id}-options-passed",
            "experiment_id": f"{cycle_id}-options-passed",
            "current_state": "paper_active",
            "state_entered_at": "2026-04-01T00:00:00+00:00",
            "last_evaluated_at": "2026-04-04T00:00:00+00:00",
            "stale_cycles": 0,
            "market": "options",
            "symbol": "SPY",
            "strategy_family": "options_fixture_alt",
            "retention": {"cycles_observed": 2, "demotion_count": 0},
        },
        {
            "candidate_id": "fixture-control-stale",
            "experiment_id": "fixture-control-stale-exp",
            "current_state": "shadow_candidate",
            "state_entered_at": "2026-04-01T00:00:00+00:00",
            "last_evaluated_at": "2026-04-04T00:00:00+00:00",
            "stale_cycles": 0,
            "market": "stocks",
            "symbol": "IWM",
            "strategy_family": "stale_family",
            "retention": {
                "cycles_observed": 2,
                "shadow_runs": 2,
                "avg_slippage_delta": 1.2,
                "avg_fill_quality": 0.05,
                "promotion_streak": 1,
                "demotion_count": 0,
                "last_feedback_status": "execution_ok",
            },
        },
    ]
    for control in controls:
        cid = str(control["candidate_id"])
        by_id[cid] = control
    payload["candidates"] = list(by_id.values())
    return payload


def main() -> None:
    cycle_id = "fixture-2026-04-05"
    output_dir = Path("runs/ai_trader/research/cycles") / cycle_id
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture = _fixture_results(cycle_id)
    (output_dir / "cycle_spec.json").write_text(
        json.dumps({"cycle_id": cycle_id, "fixture": True, "experiment_count": len(fixture)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    agent_reports_dir = output_dir / "agent_reports"
    agent_reports_dir.mkdir(parents=True, exist_ok=True)
    (agent_reports_dir / "plan.json").write_text(
        json.dumps({"fixture": True, "experiments": [r["experiment_id"] for r in fixture]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (agent_reports_dir / "data_readiness.json").write_text(
        json.dumps(
            {
                "summary": {"total": 3, "ready": 2, "degraded": 0, "blocked": 1},
                "items": [
                    {
                        "experiment_id": f"{cycle_id}-stocks-passed",
                        "status": "ready",
                        "reason": "ok",
                    },
                    {
                        "experiment_id": f"{cycle_id}-options-blocked-data",
                        "status": "blocked",
                        "reason": "no_bars_found",
                        "next_action": "widen_window_or_drop_symbol",
                    },
                    {
                        "experiment_id": f"{cycle_id}-stocks-robustness-fail",
                        "status": "ready",
                        "reason": "ok",
                    },
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    experiments_dir = output_dir / "experiments"
    experiments_dir.mkdir(parents=True, exist_ok=True)
    for result in fixture:
        (experiments_dir / f"{result['experiment_id']}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    bundle = persist_evaluation_bundle(
        fixture,
        output_dir=output_dir,
        specs_by_experiment_id={},
        cycle_id=cycle_id,
        top_k=1,
    )
    seed_specs = []
    for result in fixture:
        if result.get("status") not in {"completed", "blocked", "failed"}:
            continue
        if result.get("market") not in {"stocks", "options"}:
            continue
        seed_specs.append(
            {
                "market": result.get("market"),
                "strategy_family": result.get("strategy_family"),
                "engine": {"adapter": "stocks_kernel_backtest" if result.get("market") == "stocks" else "options_single_leg_replay", "mode": "backtest" if result.get("market") == "stocks" else "replay"},
                "universe": {"symbols": ["QQQ"] if result.get("market") == "stocks" else ["SPY"]},
                "strategy_ref": {"template_id": "qqq_intraday_core"} if result.get("market") == "stocks" else {"definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"},
                "module_selection": {"features": ["fixture_feature"], "gates": ["fixture_gate"]} if result.get("market") == "stocks" else {},
                "parameters": {},
                "data_window": {"start": "2026-03-01", "end": "2026-03-31"},
                "labels_and_ml": {},
                "robustness": {},
                "execution_reality": {},
                "sizing": {},
                "lineage": {"family_id": result.get("strategy_family")},
                "acceptance": {"min_trades": 1, "max_drawdown_pct": 50.0},
                "research_intent": "seed",
            }
        )

    # baseline tasks for this finished cycle (source tasks for feedback mapping)
    baseline_tasks = compile_agent_tasks(
        cycle_id=cycle_id,
        cycle_policy={},
        seed_specs=seed_specs,
        input_refs=[str(output_dir / "leaderboard.json")],
    )
    strategy_baseline = [t for t in baseline_tasks if t.get("task_type") == "strategy_experiment"]
    baseline_manifest = {"cycle_id": cycle_id, "task_count": len(baseline_tasks), "tasks": baseline_tasks}
    task_outcome_index = {
        "cycle_id": cycle_id,
        "outcomes": [
            {
                "task_id": strategy_baseline[0]["task_id"],
                "experiment_id": f"{cycle_id}-stocks-passed",
                "status": "completed",
                "derived_from": strategy_baseline[0].get("derived_from"),
            },
            {
                "task_id": strategy_baseline[1]["task_id"],
                "experiment_id": f"{cycle_id}-options-blocked-data",
                "status": "blocked",
                "derived_from": strategy_baseline[1].get("derived_from"),
                "defer_reason": "data_readiness_blocked",
            },
            {
                "task_id": strategy_baseline[2]["task_id"] if len(strategy_baseline) > 2 else strategy_baseline[0]["task_id"],
                "experiment_id": f"{cycle_id}-stocks-robustness-fail",
                "status": "completed",
                "derived_from": strategy_baseline[2].get("derived_from") if len(strategy_baseline) > 2 else strategy_baseline[0].get("derived_from"),
                "defer_reason": "retry_exhausted",
            },
        ],
    }
    task_schedule_plan = {
        "cycle_id": cycle_id,
        "entries": [
            {
                "task_id": strategy_baseline[0]["task_id"],
                "decision": "deferred",
                "reason": "cycle_compute_budget_exceeded",
            }
        ],
    }
    task_retry_report = {
        "cycle_id": cycle_id,
        "tasks": [
            {
                "task_id": strategy_baseline[1]["task_id"],
                "experiment_id": f"{cycle_id}-options-blocked-data",
                "final_status": "blocked",
                "defer_reason": "data_readiness_blocked",
            },
            {
                "task_id": strategy_baseline[2]["task_id"] if len(strategy_baseline) > 2 else strategy_baseline[0]["task_id"],
                "experiment_id": f"{cycle_id}-stocks-robustness-fail",
                "final_status": "failed",
                "defer_reason": "retry_exhausted",
            },
        ],
    }

    candidate_pool_artifacts = update_candidate_pool_from_evaluation(
        cycle_id=cycle_id,
        evaluation_bundle=bundle,
    )
    candidate_pool_payload = json.loads(Path(candidate_pool_artifacts["candidate_pool_json"]).read_text(encoding="utf-8"))
    portfolio_selection = build_portfolio_selection(evaluation_bundle=bundle, candidate_pool=candidate_pool_payload, max_selected=2)
    portfolio_selection_path = persist_portfolio_selection(portfolio_selection, output_dir=output_dir)
    allocation_plan = build_allocation_plan(portfolio_selection=portfolio_selection, risk_budget_bps_total=100.0, capital_total=100_000.0)
    allocation_plan_path = persist_allocation_plan(allocation_plan, output_dir=output_dir)
    promotion_gate = build_promotion_gate(candidate_results=fixture, portfolio_selection=portfolio_selection)
    promotion_gate_path = persist_promotion_gate(promotion_gate, output_dir=output_dir)
    shadow_bundle = build_shadow_candidates(
        promotion_gate=promotion_gate,
        allocation_plan=allocation_plan,
        candidate_results=fixture,
        output_dir=output_dir,
    )
    paper_bridge = build_paper_candidates(
        shadow_candidates_dir=shadow_bundle["shadow_candidates_dir"],
        output_dir=output_dir,
    )
    state_root = output_dir / "operator_state"
    state_root.mkdir(parents=True, exist_ok=True)
    operator_controls_path = persist_operator_controls(
        {
            "global_pause": True,
            "paper_only": True,
            "live_enabled": True,
            "max_live_capital": 50_000.0,
            "blocked_candidates": [f"{cycle_id}-options-passed"],
            "blocked_symbols": ["IWM"],
            "force_flatten": True,
            "cooldown_candidates": [f"{cycle_id}-stocks-passed"],
            "no_trade_windows": ["00:00-23:59"],
            "notes": "fixture_operator_control_samples",
        },
        state_root=state_root,
    )
    live_bridge = build_live_candidates(
        paper_candidates_dir=paper_bridge["paper_candidates_dir"],
        output_dir=output_dir,
        default_broker_profile="paper",
        operator_state_root=state_root,
    )
    live_feedback_index_path = Path("runs/ai_trader/research/state/live_feedback_index.json")
    live_feedback_index = {}
    if live_feedback_index_path.exists():
        live_feedback_index = json.loads(live_feedback_index_path.read_text(encoding="utf-8"))
    capital_governor_report = evaluate_capital_governor(
        cycle_id=cycle_id,
        live_candidates_dir=live_bridge["live_candidates_dir"],
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        live_feedback_index=live_feedback_index,
    )
    capital_governor_report_path = output_dir / "capital_governor_report.json"
    capital_governor_report_path.write_text(
        json.dumps(capital_governor_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    paper_manifests = paper_bridge.get("paper_manifests") or []
    simulate_kill_candidate = str(paper_manifests[0]).split("/")[-2] if paper_manifests else None
    risk_guard_report = evaluate_live_risk_guard(
        cycle_id=cycle_id,
        live_candidates_dir=live_bridge["live_candidates_dir"],
        single_trade_max_risk_bps=25.0,
        daily_max_drawdown_bps=80.0,
        max_position_notional=75_000.0,
        blacklist_symbols=[],
        cooldown_candidates=[],
        simulate_kill_switch_candidate=simulate_kill_candidate,
        operator_state_root=state_root,
    )
    risk_guard_report_path = output_dir / "risk_guard_report.json"
    risk_guard_report_path.write_text(
        json.dumps(risk_guard_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    session_control_report = evaluate_session_control(
        cycle_id=cycle_id,
        live_candidates_dir=live_bridge["live_candidates_dir"],
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        risk_guard_report=risk_guard_report,
        simulate_pre_close_flatten=True,
    )
    session_control_report_path = output_dir / "session_control_report.json"
    session_control_report_path.write_text(
        json.dumps(session_control_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    live_execution = run_live_executor(
        cycle_id=cycle_id,
        live_candidates_dir=live_bridge["live_candidates_dir"],
        risk_guard_report=risk_guard_report,
        capital_governor_report=capital_governor_report,
        session_control_report=session_control_report,
        output_dir=output_dir,
        operator_state_root=state_root,
    )
    oms = run_order_manager(
        cycle_id=cycle_id,
        live_candidates_dir=live_bridge["live_candidates_dir"],
        risk_guard_report=risk_guard_report,
        output_dir=output_dir,
        broker_profile="paper",
        operator_state_root=state_root,
    )
    account_sync = sync_account_snapshot(
        cycle_id=cycle_id,
        broker_snapshot=oms["broker_snapshot"],
        output_dir=output_dir,
    )
    pos_sync = sync_positions_snapshot(
        cycle_id=cycle_id,
        broker_snapshot=oms["broker_snapshot"],
        live_candidates_dir=live_bridge["live_candidates_dir"],
        output_dir=output_dir,
    )
    live_risk_enforce = enforce_live_risk(
        cycle_id=cycle_id,
        orders_state=oms["orders_state"],
        positions_snapshot=pos_sync["positions_snapshot"],
        output_dir=output_dir,
    )
    execution_reconcile = run_execution_reconcile(
        cycle_id=cycle_id,
        orders_state=oms["orders_state"],
        broker_snapshot=oms["broker_snapshot"],
        position_reconcile_report=pos_sync["position_reconcile_report"],
        output_dir=output_dir,
    )
    kill_switch = run_live_kill_switch(
        cycle_id=cycle_id,
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        live_risk_report=live_risk_enforce["live_risk_report"],
        positions_snapshot=pos_sync["positions_snapshot"],
        output_dir=output_dir,
    )
    recovery = run_recovery_playbook(
        cycle_id=cycle_id,
        broker_session=json.loads(Path(oms["broker_session_json"]).read_text(encoding="utf-8")),
        account_snapshot=account_sync["account_snapshot"],
        positions_snapshot=pos_sync["positions_snapshot"],
        orders_state=oms["orders_state"],
        kill_switch_report=kill_switch["kill_switch_report"],
        output_dir=output_dir,
    )
    reuse_audit = generate_live_reuse_artifacts(
        cycle_id=cycle_id,
        output_dir=output_dir,
    )
    live_feedback = build_live_feedback(
        cycle_id=cycle_id,
        live_execution_report=live_execution,
    )
    live_feedback_artifacts = persist_live_feedback(
        feedback=live_feedback,
        output_dir=output_dir,
    )
    operator_audit = persist_operator_audit(
        cycle_id=cycle_id,
        output_dir=output_dir,
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        capital_governor_report=capital_governor_report,
        session_control_report=session_control_report,
    )
    shadow_runs = run_shadow_candidates(
        shadow_candidates_dir=shadow_bundle["shadow_candidates_dir"],
        output_dir=output_dir,
    )
    shadow_execution_feedback = build_execution_feedback(
        cycle_id=cycle_id,
        shadow_runs_dir=shadow_runs["shadow_runs_dir"],
    )
    execution_feedback = {
        "cycle_id": cycle_id,
        "items": list(shadow_execution_feedback.get("items") or [])
        + list(live_feedback.get("policy_mapped_feedback") or []),
        "stats": shadow_execution_feedback.get("stats") or {},
    }
    execution_feedback_artifacts = persist_execution_feedback(
        feedback=execution_feedback,
        output_dir=output_dir,
    )
    execution_feedback_report_path = output_dir / "execution_feedback_report.json"
    execution_feedback_report_path.write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "summary": execution_feedback.get("stats") or {},
                "items": [
                    {
                        "candidate_id": row.get("candidate_id"),
                        "experiment_id": row.get("experiment_id"),
                        "feedback_status": row.get("feedback_status"),
                        "next_action": row.get("next_action"),
                    }
                    for row in execution_feedback.get("items", [])
                    if isinstance(row, dict)
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lifecycle_state_root = Path("runs/ai_trader/research/state")
    loaded_lifecycle = _seed_fixture_lifecycle_controls(
        load_candidate_lifecycle_index(state_root=lifecycle_state_root),
        cycle_id=cycle_id,
    )
    candidate_retention_report = build_candidate_retention(
        cycle_id=cycle_id,
        execution_feedback=execution_feedback,
        lifecycle_index=loaded_lifecycle,
    )
    candidate_gate_report = build_candidate_gate(
        cycle_id=cycle_id,
        lifecycle_index=loaded_lifecycle,
        promotion_gate=promotion_gate,
        execution_feedback=execution_feedback,
        retention_report=candidate_retention_report,
        min_cycles_for_promote=1,
    )
    lifecycle_apply = apply_candidate_lifecycle(
        cycle_id=cycle_id,
        lifecycle_index=loaded_lifecycle,
        candidate_gate_report=candidate_gate_report,
        candidate_retention_report=candidate_retention_report,
    )
    candidate_lifecycle_index = lifecycle_apply["index"]
    candidate_lifecycle_report = lifecycle_apply["report"]
    candidate_lifecycle_index_path = persist_candidate_lifecycle_index(
        index=candidate_lifecycle_index,
        state_root=lifecycle_state_root,
    )
    candidate_retention_report_path = output_dir / "candidate_retention_report.json"
    candidate_gate_report_path = output_dir / "candidate_gate_report.json"
    candidate_lifecycle_report_path = output_dir / "candidate_lifecycle_report.json"
    candidate_retention_report_path.write_text(
        json.dumps(candidate_retention_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    candidate_gate_report_path.write_text(
        json.dumps(candidate_gate_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    candidate_lifecycle_report_path.write_text(
        json.dumps(candidate_lifecycle_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    admission_gate = build_admission_gate(
        cycle_id=cycle_id,
        candidate_lifecycle_index=candidate_lifecycle_index,
        candidate_retention_report=candidate_retention_report,
        risk_guard_report=risk_guard_report,
        min_cycles_observed=1,
        min_shadow_runs=1,
        max_avg_slippage_bps=10.0,
        min_fill_rate=0.75,
    )
    health_monitor_report = build_health_monitor_report(
        cycle_id=cycle_id,
        live_execution_report=live_execution,
        live_feedback=live_feedback,
        risk_guard_report=risk_guard_report,
    )
    auto_halt_report = build_auto_halt_report(
        cycle_id=cycle_id,
        health_monitor_report=health_monitor_report,
        admission_gate=admission_gate,
    )
    existing_registry = load_active_registry(state_root=lifecycle_state_root)
    registry_update = update_active_registry(
        cycle_id=cycle_id,
        existing_registry=existing_registry,
        admission_gate=admission_gate,
        health_monitor_report=health_monitor_report,
        auto_halt_report=auto_halt_report,
    )
    active_registry_path = persist_active_registry(
        registry=registry_update["registry"],
        state_root=lifecycle_state_root,
    )
    active_registry_report = registry_update["report"]
    admission_gate_path = output_dir / "admission_gate.json"
    health_monitor_report_path = output_dir / "health_monitor_report.json"
    auto_halt_report_path = output_dir / "auto_halt_report.json"
    active_registry_report_path = output_dir / "active_registry_report.json"
    admission_gate_path.write_text(json.dumps(admission_gate, indent=2, ensure_ascii=False), encoding="utf-8")
    health_monitor_report_path.write_text(
        json.dumps(health_monitor_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    auto_halt_report_path.write_text(json.dumps(auto_halt_report, indent=2, ensure_ascii=False), encoding="utf-8")
    active_registry_report_path.write_text(
        json.dumps(active_registry_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    policy_context = _load_preflight_policy_context(
        state_root=lifecycle_state_root,
        cycle_dir=output_dir,
    )
    preflight_check = build_preflight_check(
        cycle_id=cycle_id,
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        capital_governor_report=capital_governor_report,
        session_control_report=session_control_report,
        admission_gate=admission_gate,
        health_monitor_report=health_monitor_report,
        active_registry=registry_update["registry"],
        candidate_lifecycle_index=candidate_lifecycle_index,
        live_feedback_index=json.loads(Path(live_feedback_artifacts["live_feedback_index_json"]).read_text(encoding="utf-8")),
        required_artifacts=[
            str(operator_controls_path),
            str(capital_governor_report_path),
            str(session_control_report_path),
            str(admission_gate_path),
            str(health_monitor_report_path),
            str(active_registry_path),
            str(candidate_lifecycle_index_path),
            str(live_feedback_artifacts["live_feedback_index_json"]),
            str(bundle["artifacts"]["leaderboard_json"]),
            str(promotion_gate_path),
        ],
        evidence_integrity_report=build_evidence_integrity_report(
            cycle_id=cycle_id,
            evidence_paths={
                "admission_gate": str(admission_gate_path),
                "health_monitor": str(health_monitor_report_path),
                "operator_controls": str(operator_controls_path),
                "capital_governor": str(capital_governor_report_path),
                "session_control": str(session_control_report_path),
                "execution_feedback": str(execution_feedback_artifacts["execution_feedback_json"]),
                "candidate_lifecycle": str(candidate_lifecycle_index_path),
            },
        ),
        preflight_policy=policy_context.get("preflight_policy"),
        preflight_history=policy_context.get("preflight_history"),
    )
    policy_compatibility_summary = persist_policy_compatibility_summary(
        cycle_id=cycle_id,
        preflight_check=preflight_check,
        state_root=lifecycle_state_root,
        cycles_root=Path("runs/ai_trader/research/cycles"),
    )
    policy_observability_consumers = persist_policy_observability_consumer_inventory(
        state_root=lifecycle_state_root,
        repo_root=Path("."),
    )
    readiness_dashboard = build_readiness_dashboard(
        cycle_id=cycle_id,
        preflight_check=preflight_check,
        admission_gate=admission_gate,
        health_monitor_report=health_monitor_report,
        active_registry=registry_update["registry"],
        policy_compatibility_summary_path=policy_compatibility_summary["policy_compatibility_summary_json"],
        policy_observability_consumers_path=policy_observability_consumers["policy_observability_consumers_json"],
    )
    launch_checklist = build_launch_checklist(
        cycle_id=cycle_id,
        preflight_check=preflight_check,
        readiness_dashboard=readiness_dashboard,
    )
    launch_verdict = build_launch_verdict(
        cycle_id=cycle_id,
        preflight_check=preflight_check,
        launch_checklist=launch_checklist,
        readiness_dashboard=readiness_dashboard,
        admission_gate=admission_gate,
        policy_compatibility_summary_path=policy_compatibility_summary["policy_compatibility_summary_json"],
        policy_observability_consumers_path=policy_observability_consumers["policy_observability_consumers_json"],
    )
    broker_safety_gate_bundle = build_broker_safety_gate(
        cycle_id=cycle_id,
        broker_profile_report=json.loads(Path(oms["broker_profile_report_json"]).read_text(encoding="utf-8")),
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        preflight_check=preflight_check,
        admission_gate=admission_gate,
        health_monitor_report=health_monitor_report,
        kill_switch_report=kill_switch["kill_switch_report"],
        output_dir=output_dir,
    )
    broker_safety_gate_path = Path(broker_safety_gate_bundle["broker_safety_gate_json"])
    evidence_integrity_report = build_evidence_integrity_report(
        cycle_id=cycle_id,
        evidence_paths={
            "admission_gate": str(admission_gate_path),
            "health_monitor": str(health_monitor_report_path),
            "operator_controls": str(operator_controls_path),
            "capital_governor": str(capital_governor_report_path),
            "session_control": str(session_control_report_path),
            "execution_feedback": str(execution_feedback_artifacts["execution_feedback_json"]),
            "candidate_lifecycle": str(candidate_lifecycle_index_path),
        },
    )
    launch_batch = build_launch_batch(
        cycle_id=cycle_id,
        mode="small_live" if str(launch_verdict.get("verdict")) == "go_small_live" else "paper",
        launch_verdict=launch_verdict,
        admission_gate=admission_gate,
        allocation_plan=json.loads(Path(allocation_plan_path).read_text(encoding="utf-8")),
        portfolio_selection=json.loads(Path(portfolio_selection_path).read_text(encoding="utf-8")),
        evidence_integrity_report=evidence_integrity_report,
        min_candidates=2,
    )
    launch_registry_path = persist_launch_registry(
        cycle_id=cycle_id,
        launch_verdict=launch_verdict,
        launch_batch=launch_batch,
        active_registry=registry_update["registry"],
        state_root=lifecycle_state_root,
    )
    system_status_path = persist_system_status(
        cycle_id=cycle_id,
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        risk_guard_report=risk_guard_report,
        launch_verdict=launch_verdict,
        state_root=lifecycle_state_root,
    )
    broker_readonly_probe = run_broker_readonly_probe(
        cycle_id=cycle_id,
        broker=oms.get("broker_adapter"),
        broker_profile=str(json.loads(Path(oms["broker_profile_report_json"]).read_text(encoding="utf-8")).get("broker_profile") or "paper"),
        output_dir=output_dir,
    )
    broker_sandbox = run_broker_sandbox_orders(
        cycle_id=cycle_id,
        broker_profile_report=json.loads(Path(oms["broker_profile_report_json"]).read_text(encoding="utf-8")),
        broker_safety_gate=json.loads(Path(broker_safety_gate_path).read_text(encoding="utf-8")),
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        risk_guard_report=risk_guard_report,
        capital_governor_report=capital_governor_report,
        session_control_report=session_control_report,
        output_dir=output_dir,
    )
    broker_reconcile = run_broker_reconcile(
        cycle_id=cycle_id,
        orders_state=oms["orders_state"],
        broker_snapshot=oms["broker_snapshot"],
        positions_snapshot=pos_sync["positions_snapshot"],
        output_dir=output_dir,
    )
    eod_closeout = run_eod_closeout(
        cycle_id=cycle_id,
        broker_snapshot=oms["broker_snapshot"],
        kill_switch_report=kill_switch["kill_switch_report"],
        output_dir=output_dir,
    )
    broker_recovery = run_broker_recovery(
        cycle_id=cycle_id,
        broker=oms.get("broker_adapter"),
        broker_session=json.loads(Path(oms["broker_session_json"]).read_text(encoding="utf-8")),
        output_dir=output_dir,
    )
    broker_launch_barrier = apply_broker_launch_barrier(
        cycle_id=cycle_id,
        broker_readonly_probe=broker_readonly_probe["broker_readonly_probe"],
        broker_reconcile_report=broker_reconcile["broker_reconcile_report"],
        eod_closeout_report=eod_closeout["eod_closeout_report"],
        launch_registry=json.loads(Path(launch_registry_path).read_text(encoding="utf-8")),
        active_registry=json.loads(Path(active_registry_path).read_text(encoding="utf-8")),
        system_status=json.loads(Path(system_status_path).read_text(encoding="utf-8")),
        state_root=lifecycle_state_root,
        output_dir=output_dir,
    )
    persist_manual_approval(
        {
            "approvals": [
                {
                    "candidate_id": "fixture-2026-04-05-stocks-passed",
                    "approved": True,
                    "approved_by": "fixture_operator",
                    "approved_at": "2026-04-05T13:00:00+00:00",
                    "max_live_capital": 300.0,
                    "expires_at": "2026-04-05T23:59:00+00:00",
                    "notes": "fixture_canary_approval",
                },
                {
                    "candidate_id": "fixture-expired-candidate",
                    "approved": True,
                    "approved_by": "fixture_operator",
                    "approved_at": "2026-04-04T13:00:00+00:00",
                    "max_live_capital": 100.0,
                    "expires_at": "2026-04-04T23:59:00+00:00",
                    "notes": "expired_fixture_approval",
                },
            ]
        },
        state_root=lifecycle_state_root,
    )
    manual_approval = build_manual_approval_report(
        cycle_id=cycle_id,
        candidate_id="fixture-2026-04-05-stocks-passed",
        output_dir=output_dir,
        state_root=lifecycle_state_root,
    )
    canary_gate = build_canary_launch_gate(
        cycle_id=cycle_id,
        candidate_id="fixture-2026-04-05-stocks-passed",
        launch_verdict=launch_verdict,
        broker_safety_gate=json.loads(Path(broker_safety_gate_path).read_text(encoding="utf-8")),
        broker_launch_barrier=broker_launch_barrier["broker_launch_barrier"],
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        broker_launch_switch=json.loads(Path(oms["broker_launch_switch_json"]).read_text(encoding="utf-8")),
        manual_approval_report=manual_approval["manual_approval_report"],
        candidate_lifecycle_index=json.loads(Path(candidate_lifecycle_index_path).read_text(encoding="utf-8")),
        output_dir=output_dir,
    )
    canary_exec = run_canary_executor(
        cycle_id=cycle_id,
        candidate_id="fixture-2026-04-05-stocks-passed",
        symbol="QQQ",
        canary_launch_gate=canary_gate["canary_launch_gate"],
        manual_approval_report=manual_approval["manual_approval_report"],
        output_dir=output_dir,
    )
    intraday_monitor = build_intraday_monitor_report(
        cycle_id=cycle_id,
        canary_run_summary=canary_exec["canary_run_summary"],
        output_dir=output_dir,
    )
    post_trade_attribution = build_post_trade_attribution(
        cycle_id=cycle_id,
        canary_run_summary=canary_exec["canary_run_summary"],
        intraday_monitor_report=intraday_monitor["intraday_monitor_report"],
        output_dir=output_dir,
    )
    canary_feedback = apply_canary_feedback(
        cycle_id=cycle_id,
        candidate_id="fixture-2026-04-05-stocks-passed",
        post_trade_attribution=post_trade_attribution["post_trade_attribution"],
        intraday_monitor_report=intraday_monitor["intraday_monitor_report"],
        candidate_lifecycle_index=json.loads(Path(candidate_lifecycle_index_path).read_text(encoding="utf-8")),
        active_registry=json.loads(Path(active_registry_path).read_text(encoding="utf-8")),
        launch_registry=json.loads(Path(launch_registry_path).read_text(encoding="utf-8")),
        system_status=json.loads(Path(system_status_path).read_text(encoding="utf-8")),
        state_root=lifecycle_state_root,
        output_dir=output_dir,
    )
    live_batch = build_live_batch_plan(
        cycle_id=cycle_id,
        launch_batch=launch_batch,
        admission_gate=admission_gate,
        capital_governor_report=capital_governor_report,
        risk_guard_report=risk_guard_report,
        mode="small_live",
        output_dir=output_dir,
    )
    pre_open_prep = run_pre_open_prep(
        cycle_id=cycle_id,
        live_batch_plan=live_batch["live_batch_plan"],
        manual_approvals=json.loads((Path(lifecycle_state_root) / "manual_approval.json").read_text(encoding="utf-8")),
        broker_readonly_probe=broker_readonly_probe["broker_readonly_probe"],
        account_snapshot=account_sync["account_snapshot"],
        positions_snapshot=pos_sync["positions_snapshot"],
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        session_control_report=session_control_report,
        kill_switch_report=kill_switch["kill_switch_report"],
        output_dir=output_dir,
    )
    intraday_batch = run_intraday_batch_supervisor(
        cycle_id=cycle_id,
        live_batch_plan=live_batch["live_batch_plan"],
        pre_open_prep_report=pre_open_prep["pre_open_prep_report"],
        risk_guard_report=risk_guard_report,
        output_dir=output_dir,
    )
    eod_batch_close = run_eod_batch_close(
        cycle_id=cycle_id,
        live_batch_plan=live_batch["live_batch_plan"],
        intraday_batch_report=intraday_batch["intraday_batch_report"],
        broker_reconcile_report=broker_reconcile["broker_reconcile_report"],
        output_dir=output_dir,
    )
    live_ops_summary = build_live_ops_summary(
        cycle_id=cycle_id,
        live_batch_plan=live_batch["live_batch_plan"],
        pre_open_prep_report=pre_open_prep["pre_open_prep_report"],
        intraday_batch_report=intraday_batch["intraday_batch_report"],
        eod_batch_close_report=eod_batch_close["eod_batch_close_report"],
        output_dir=output_dir,
    )
    live_batch_feedback = apply_live_batch_feedback(
        cycle_id=cycle_id,
        live_batch_plan=live_batch["live_batch_plan"],
        intraday_batch_report=intraday_batch["intraday_batch_report"],
        eod_batch_close_report=eod_batch_close["eod_batch_close_report"],
        candidate_lifecycle_index=json.loads(Path(candidate_lifecycle_index_path).read_text(encoding="utf-8")),
        active_registry=json.loads(Path(active_registry_path).read_text(encoding="utf-8")),
        launch_registry=json.loads(Path(launch_registry_path).read_text(encoding="utf-8")),
        state_root=lifecycle_state_root,
        output_dir=output_dir,
    )
    trading_day_status = build_trading_day_status(
        day_id=cycle_id,
        pre_open_prep_report=pre_open_prep["pre_open_prep_report"],
        intraday_batch_report=intraday_batch["intraday_batch_report"],
        eod_batch_close_report=eod_batch_close["eod_batch_close_report"],
        output_dir=output_dir,
    )
    ops_control_center = build_ops_control_center(
        day_id=cycle_id,
        live_batch_plan=live_batch["live_batch_plan"],
        live_batch_status=live_batch["live_batch_status"],
        intraday_batch_report=intraday_batch["intraday_batch_report"],
        live_ops_summary=live_ops_summary["live_ops_summary"],
        capital_governor_report=capital_governor_report,
        operator_controls=json.loads(Path(operator_controls_path).read_text(encoding="utf-8")),
        output_dir=output_dir,
    )
    end_of_day_review = build_end_of_day_review(
        day_id=cycle_id,
        live_batch_plan=live_batch["live_batch_plan"],
        intraday_batch_report=intraday_batch["intraday_batch_report"],
        eod_batch_close_report=eod_batch_close["eod_batch_close_report"],
        live_ops_summary=live_ops_summary["live_ops_summary"],
        output_dir=output_dir,
    )
    weekly_candidate_review = build_weekly_candidate_review(
        week_id=cycle_id,
        candidate_lifecycle_index=json.loads(Path(candidate_lifecycle_index_path).read_text(encoding="utf-8")),
        candidate_retention_report=json.loads(Path(candidate_retention_report_path).read_text(encoding="utf-8")),
        live_feedback_index=json.loads(Path(live_feedback_artifacts["live_feedback_index_json"]).read_text(encoding="utf-8")),
        output_dir=output_dir,
    )
    review_plan_patch = build_review_plan_patch(
        day_id=cycle_id,
        end_of_day_review=end_of_day_review["end_of_day_review"],
        weekly_candidate_review=weekly_candidate_review["weekly_candidate_review"],
        output_dir=output_dir,
    )
    human_ops_summary = build_human_ops_summary(
        day_id=cycle_id,
        trading_day_status=trading_day_status["trading_day_status"],
        ops_control_center=ops_control_center["ops_control_center"],
        end_of_day_review=end_of_day_review["end_of_day_review"],
        weekly_candidate_review=weekly_candidate_review["weekly_candidate_review"],
        review_plan_patch=review_plan_patch["review_plan_patch"],
        output_dir=output_dir,
        policy_compatibility_summary_path=policy_compatibility_summary["policy_compatibility_summary_json"],
        policy_observability_consumers_path=policy_observability_consumers["policy_observability_consumers_json"],
    )
    incident_artifacts = build_incident_artifacts(
        day_id=cycle_id,
        run_id=cycle_id,
        batch_id=str(live_batch["live_batch_plan"].get("batch_id") or ""),
        intraday_batch_report=intraday_batch["intraday_batch_report"],
        broker_readonly_probe=broker_readonly_probe["broker_readonly_probe"],
        broker_reconcile_report=broker_reconcile["broker_reconcile_report"],
        kill_switch_report=kill_switch["kill_switch_report"],
        output_dir=output_dir,
        state_root=lifecycle_state_root,
    )
    recovery_coordinator = build_recovery_coordinator_report(
        day_id=cycle_id,
        run_id=cycle_id,
        incident_index=incident_artifacts["incident_index"],
        trading_day_status=trading_day_status["trading_day_status"],
        output_dir=output_dir,
    )
    multi_day_runner = build_multi_day_run_artifacts(
        day_id=cycle_id,
        run_id=cycle_id,
        trading_day_status=trading_day_status["trading_day_status"],
        incident_index=incident_artifacts["incident_index"],
        recovery_coordinator_report=recovery_coordinator["recovery_coordinator_report"],
        output_dir=output_dir,
    )
    reliability_metrics = build_reliability_metrics(
        day_id=cycle_id,
        run_id=cycle_id,
        multi_day_run_report=multi_day_runner["multi_day_run_report"],
        incident_index=incident_artifacts["incident_index"],
        broker_readonly_probe=broker_readonly_probe["broker_readonly_probe"],
        broker_reconcile_report=broker_reconcile["broker_reconcile_report"],
        recovery_coordinator_report=recovery_coordinator["recovery_coordinator_report"],
        live_ops_summary=live_ops_summary["live_ops_summary"],
        output_dir=output_dir,
    )
    operator_shift_summary = build_operator_shift_summary(
        day_id=cycle_id,
        run_id=cycle_id,
        incident_index=incident_artifacts["incident_index"],
        active_registry=json.loads(Path(active_registry_path).read_text(encoding="utf-8")),
        ops_control_center=ops_control_center["ops_control_center"],
        recovery_coordinator_report=recovery_coordinator["recovery_coordinator_report"],
        output_dir=output_dir,
    )
    ops_registry = update_ops_registry(
        day_id=cycle_id,
        run_id=cycle_id,
        system_mode=str(live_batch["live_batch_plan"].get("mode") or "paper"),
        trading_day_status=trading_day_status["trading_day_status"],
        launch_status=launch_verdict,
        ops_control_center=ops_control_center["ops_control_center"],
        incident_index=incident_artifacts["incident_index"],
        reliability_metrics=reliability_metrics["reliability_metrics"],
        state_root=lifecycle_state_root,
    )
    execution_feedback["items"] = list(execution_feedback.get("items") or []) + [
        canary_feedback["execution_feedback_item"]
    ] + list(live_batch_feedback["live_batch_feedback_report"].get("feedback_items") or [])
    execution_feedback_artifacts = persist_execution_feedback(
        feedback=execution_feedback,
        output_dir=output_dir,
    )
    candidate_lifecycle_index_path = str(Path(lifecycle_state_root) / "candidate_lifecycle_index.json")
    active_registry_path = str(Path(lifecycle_state_root) / "active_registry.json")
    launch_registry_path = str(Path(lifecycle_state_root) / "launch_registry.json")
    system_status_path = str(Path(lifecycle_state_root) / "system_status.json")
    ops_summary = build_ops_summary(
        cycle_id=cycle_id,
        active_registry=json.loads(Path(active_registry_path).read_text(encoding="utf-8")),
        auto_halt_report=auto_halt_report,
        candidate_lifecycle_report=candidate_lifecycle_report,
        live_feedback=live_feedback,
    )
    preflight_check_path = output_dir / "preflight_check.json"
    postflight_check_path = output_dir / "postflight_check.json"
    acceptance_decision_path = output_dir / "acceptance_decision.json"
    artifact_index_path = output_dir / "artifact_index.json"
    consistency_report_path = output_dir / "consistency_report.json"
    launch_checklist_path = output_dir / "launch_checklist.json"
    readiness_dashboard_path = output_dir / "readiness_dashboard.json"
    launch_verdict_path = output_dir / "launch_verdict.json"
    evidence_integrity_report_path = output_dir / "evidence_integrity_report.json"
    launch_batch_path = output_dir / "launch_batch.json"
    ops_summary_path = output_dir / "ops_summary.json"
    write_json_crash_safe(preflight_check_path, preflight_check)
    launch_checklist_path.write_text(json.dumps(launch_checklist, indent=2, ensure_ascii=False), encoding="utf-8")
    readiness_dashboard_path.write_text(json.dumps(readiness_dashboard, indent=2, ensure_ascii=False), encoding="utf-8")
    launch_verdict_path.write_text(json.dumps(launch_verdict, indent=2, ensure_ascii=False), encoding="utf-8")
    evidence_integrity_report_path.write_text(
        json.dumps(evidence_integrity_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    launch_batch_path.write_text(json.dumps(launch_batch, indent=2, ensure_ascii=False), encoding="utf-8")
    ops_summary_path.write_text(json.dumps(ops_summary, indent=2, ensure_ascii=False), encoding="utf-8")
    artifact_index_bundle = build_artifact_index(
        cycle_id=cycle_id,
        run_id=cycle_id,
        cycle_dir=output_dir,
        state_root=lifecycle_state_root,
        required_artifacts={
            "preflight_check": preflight_check_path,
            "launch_verdict": launch_verdict_path,
            "evidence_integrity_report": evidence_integrity_report_path,
            "incident_index": incident_artifacts["incident_index_json"],
            "recovery_coordinator_report": recovery_coordinator["recovery_coordinator_report_json"],
            "operator_shift_summary": operator_shift_summary["operator_shift_summary_json"],
            "reliability_metrics": reliability_metrics["reliability_metrics_json"],
            "ops_registry": ops_registry["ops_registry_json"],
        },
    )
    artifact_index = artifact_index_bundle["artifact_index"]
    consistency_bundle = build_consistency_report(
        cycle_id=cycle_id,
        run_id=cycle_id,
        incident_index=incident_artifacts["incident_index"],
        recovery_report=recovery_coordinator["recovery_coordinator_report"],
        shift_summary=operator_shift_summary["operator_shift_summary"],
        reliability_metrics=reliability_metrics["reliability_metrics"],
        ops_registry=ops_registry["ops_registry"],
        artifact_index=artifact_index,
        output_dir=output_dir,
    )
    postflight_bundle = build_postflight_check(
        cycle_id=cycle_id,
        run_id=cycle_id,
        preflight_check=preflight_check,
        consistency_report=consistency_bundle["consistency_report"],
        recovery_report=recovery_coordinator["recovery_coordinator_report"],
        incident_index=incident_artifacts["incident_index"],
        reliability_metrics=reliability_metrics["reliability_metrics"],
        output_dir=output_dir,
    )
    acceptance_bundle = build_acceptance_decision(
        cycle_id=cycle_id,
        run_id=cycle_id,
        preflight_check=preflight_check,
        postflight_check=postflight_bundle["postflight_check"],
        reliability_metrics=reliability_metrics["reliability_metrics"],
        incident_index=incident_artifacts["incident_index"],
        output_dir=output_dir,
    )
    next_policy = persist_next_cycle_policy(
        bundle,
        output_dir=output_dir,
        cycle_id=cycle_id,
        task_outcome_index=task_outcome_index,
        task_schedule_plan=task_schedule_plan,
        task_retry_report=task_retry_report,
        task_manifest=baseline_manifest,
        execution_feedback=execution_feedback,
        candidate_lifecycle_index=candidate_lifecycle_index,
    )
    tasks = compile_agent_tasks(
        cycle_id=cycle_id,
        cycle_policy=next_policy["policy"],
        seed_specs=seed_specs,
        input_refs=[str(output_dir / "next_cycle_policy.json")],
    )
    task_artifacts = persist_agent_tasks(tasks, cycle_dir=output_dir)
    (output_dir / "task_manifest.json").write_text(
        json.dumps({"cycle_id": cycle_id, "task_count": len(tasks), "tasks": tasks}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print("fixture_cycle_dir:", output_dir)
    print("leaderboard:", bundle["artifacts"]["leaderboard_json"])
    print("blocked_summary:", bundle["artifacts"]["blocked_summary_json"])
    print("quality_gate_breakdown:", bundle["artifacts"]["quality_gate_breakdown_json"])
    print("next_cycle_policy:", next_policy["path"])
    print("feedback_mapping_report:", next_policy["feedback_mapping_report_path"])
    print("portfolio_selection:", portfolio_selection_path)
    print("allocation_plan:", allocation_plan_path)
    print("promotion_gate:", promotion_gate_path)
    print("shadow_candidates:", shadow_bundle["shadow_candidates_dir"])
    print("paper_candidates:", paper_bridge["paper_candidates_dir"])
    print("live_candidates:", live_bridge["live_candidates_dir"])
    print("operator_controls:", operator_controls_path)
    print("capital_governor_report:", capital_governor_report_path)
    print("risk_guard_report:", risk_guard_report_path)
    print("session_control_report:", session_control_report_path)
    print("operator_audit_log:", operator_audit["operator_audit_log_jsonl"])
    print("control_decision_index:", operator_audit["control_decision_index_json"])
    print("live_runs:", live_execution["live_runs_dir"])
    print("live_feedback:", live_feedback_artifacts["live_feedback_json"])
    print("live_feedback_index:", live_feedback_artifacts["live_feedback_index_json"])
    print("shadow_runs:", shadow_runs["shadow_runs_dir"])
    print("execution_feedback:", execution_feedback_artifacts["execution_feedback_json"])
    print("execution_feedback_report:", execution_feedback_report_path)
    print("candidate_lifecycle_index:", candidate_lifecycle_index_path)
    print("candidate_gate_report:", candidate_gate_report_path)
    print("candidate_retention_report:", candidate_retention_report_path)
    print("candidate_lifecycle_report:", candidate_lifecycle_report_path)
    print("admission_gate:", admission_gate_path)
    print("health_monitor_report:", health_monitor_report_path)
    print("auto_halt_report:", auto_halt_report_path)
    print("active_registry:", active_registry_path)
    print("active_registry_report:", active_registry_report_path)
    print("preflight_check:", preflight_check_path)
    print("postflight_check:", postflight_bundle["postflight_check_json"])
    print("acceptance_decision:", acceptance_bundle["acceptance_decision_json"])
    print("artifact_index:", artifact_index_bundle["artifact_index_json"])
    print("consistency_report:", consistency_bundle["consistency_report_json"])
    print("launch_checklist:", launch_checklist_path)
    print("readiness_dashboard:", readiness_dashboard_path)
    print("launch_verdict:", launch_verdict_path)
    print("evidence_integrity_report:", evidence_integrity_report_path)
    print("launch_batch:", launch_batch_path)
    print("ops_summary:", ops_summary_path)
    print("policy_compatibility_summary:", policy_compatibility_summary["policy_compatibility_summary_json"])
    print("policy_observability_consumers:", policy_observability_consumers["policy_observability_consumers_json"])
    print("launch_registry:", launch_registry_path)
    print("system_status:", system_status_path)
    print("broker_session:", oms["broker_session_json"])
    print("orders_state:", oms["orders_state_json"])
    print("orders_events:", oms["orders_events_jsonl"])
    print("broker_profile_report:", oms["broker_profile_report_json"])
    print("broker_binding_report:", oms["broker_binding_report_json"])
    print("real_broker_dry_run:", oms["real_broker_dry_run_json"])
    print("broker_launch_switch:", oms["broker_launch_switch_json"])
    print("broker_safety_gate:", broker_safety_gate_path)
    print("broker_readonly_probe:", broker_readonly_probe["broker_readonly_probe_json"])
    print("broker_sandbox_orders:", broker_sandbox["broker_sandbox_orders_json"])
    print("broker_sandbox_events:", broker_sandbox["broker_sandbox_events_jsonl"])
    print("broker_reconcile_report:", broker_reconcile["broker_reconcile_report_json"])
    print("eod_closeout_report:", eod_closeout["eod_closeout_report_json"])
    print("broker_recovery_report:", broker_recovery["broker_recovery_report_json"])
    print("broker_launch_barrier:", broker_launch_barrier["broker_launch_barrier_json"])
    print("canary_launch_gate:", canary_gate["canary_launch_gate_json"])
    print("manual_approval_report:", manual_approval["manual_approval_report_json"])
    print("canary_run_summary:", canary_exec["canary_run_summary_json"])
    print("intraday_monitor_report:", intraday_monitor["intraday_monitor_report_json"])
    print("post_trade_attribution:", post_trade_attribution["post_trade_attribution_json"])
    print("canary_feedback_report:", canary_feedback["canary_feedback_report_json"])
    print("live_batch_plan:", live_batch["live_batch_plan_json"])
    print("live_batch_status:", live_batch["live_batch_status_json"])
    print("pre_open_prep_report:", pre_open_prep["pre_open_prep_report_json"])
    print("intraday_batch_report:", intraday_batch["intraday_batch_report_json"])
    print("eod_batch_close_report:", eod_batch_close["eod_batch_close_report_json"])
    print("live_ops_summary:", live_ops_summary["live_ops_summary_json"])
    print("live_batch_feedback_report:", live_batch_feedback["live_batch_feedback_report_json"])
    print("trading_day_status:", trading_day_status["trading_day_status_json"])
    print("ops_control_center:", ops_control_center["ops_control_center_json"])
    print("end_of_day_review:", end_of_day_review["end_of_day_review_json"])
    print("weekly_candidate_review:", weekly_candidate_review["weekly_candidate_review_json"])
    print("review_plan_patch:", review_plan_patch["review_plan_patch_json"])
    print("human_ops_summary:", human_ops_summary["human_ops_summary_json"])
    print("multi_day_run_plan:", multi_day_runner["multi_day_run_plan_json"])
    print("multi_day_run_report:", multi_day_runner["multi_day_run_report_json"])
    print("incident_log:", incident_artifacts["incident_log_jsonl"])
    print("incident_index:", incident_artifacts["incident_index_json"])
    print("recovery_coordinator_report:", recovery_coordinator["recovery_coordinator_report_json"])
    print("operator_shift_summary:", operator_shift_summary["operator_shift_summary_json"])
    print("reliability_metrics:", reliability_metrics["reliability_metrics_json"])
    print("ops_registry:", ops_registry["ops_registry_json"])
    print("account_snapshot:", account_sync["account_snapshot_json"])
    print("positions_snapshot:", pos_sync["positions_snapshot_json"])
    print("position_reconcile_report:", pos_sync["position_reconcile_report_json"])
    print("live_risk_report:", live_risk_enforce["live_risk_report_json"])
    print("execution_reconcile_report:", execution_reconcile["execution_reconcile_report_json"])
    print("kill_switch_report:", kill_switch["kill_switch_report_json"])
    print("recovery_report:", recovery["recovery_report_json"])
    print("live_capability_inventory:", reuse_audit["live_capability_inventory_json"])
    print("live_reuse_plan:", reuse_audit["live_reuse_plan_json"])
    print("live_integration_report:", reuse_audit["live_integration_report_json"])
    print("live_gap_list:", reuse_audit["live_gap_list_json"])
    print("agent_tasks:", task_artifacts["tasks_json"])
    print("candidate_pool:", candidate_pool_artifacts["candidate_pool_json"])


if __name__ == "__main__":
    main()
