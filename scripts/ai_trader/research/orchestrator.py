from __future__ import annotations

import logging
import json
import os
from typing import Dict, Any, List
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from scripts.ai_trader.research import experiment_api
from scripts.ai_trader.research.agent_tasking import compile_agent_tasks, load_agent_tasks, persist_agent_tasks
from scripts.ai_trader.research.candidate_pool import update_candidate_pool_from_evaluation
from scripts.ai_trader.research.cycle_state import CycleStateStore
from scripts.ai_trader.research.cycle_policy import persist_next_cycle_policy
from scripts.ai_trader.research.evaluator import persist_evaluation_bundle
from scripts.ai_trader.research.portfolio_selection import build_portfolio_selection, persist_portfolio_selection
from scripts.ai_trader.research.promotion_gate import build_promotion_gate, persist_promotion_gate
from scripts.ai_trader.research.shadow_bundle import build_allocation_plan, build_shadow_candidates, persist_allocation_plan
from scripts.ai_trader.research.candidate_gate import build_candidate_gate
from scripts.ai_trader.research.candidate_lifecycle import (
    apply_candidate_lifecycle,
    load_candidate_lifecycle_index,
    persist_candidate_lifecycle_index,
)
from scripts.ai_trader.research.candidate_retention import build_candidate_retention
from scripts.ai_trader.research.paper_bridge import build_paper_candidates
from scripts.ai_trader.research.shadow_runner import run_shadow_candidates
from scripts.ai_trader.research.execution_feedback import build_execution_feedback, persist_execution_feedback
from scripts.ai_trader.research.live_bridge import build_live_candidates
from scripts.ai_trader.research.capital_governor import evaluate_capital_governor
from scripts.ai_trader.research.live_risk_guard import evaluate_live_risk_guard
from scripts.ai_trader.research.live_executor import run_live_executor
from scripts.ai_trader.research.live_feedback import build_live_feedback, persist_live_feedback
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
from scripts.ai_trader.research.manual_approval import build_manual_approval_report
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
from scripts.ai_trader.research.operator_controls import load_operator_controls
from scripts.ai_trader.research.session_control import evaluate_session_control
from scripts.ai_trader.research.operator_audit import persist_operator_audit
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
from tools.nightly_oof_flow import publish_ml_event

# Use real scheduler enqueue if available
try:
    from scripts.ai_trader.ml_scheduler import enqueue_job  # type: ignore
except Exception:
    enqueue_job = None  # type: ignore

try:
    from scripts.ai_trader import ml_scheduler  # type: ignore
except Exception:
    ml_scheduler = None  # type: ignore

logger = logging.getLogger(__name__)

RESEARCH_CYCLES_ROOT = Path(
    os.getenv("AI_TRADER_RESEARCH_CYCLES_ROOT", "runs/ai_trader/research/cycles")
)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _priority_by_intent(research_intent: str) -> int:
    intent = str(research_intent or "").strip().lower()
    mapping = {
        "promote_candidate": 100,
        "lineage_expand": 95,
        "retest_robustness": 90,
        "retest_data": 85,
        "seed": 70,
        "portfolio_diversify": 60,
    }
    return int(mapping.get(intent, 65))


def _load_json_safe(path: str | Path) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _load_first_json(paths: List[Path]) -> Dict[str, Any]:
    for path in paths:
        payload = _load_json_safe(path)
        if payload:
            return payload
    return {}


def _load_preflight_policy_context(*, state_root: Path, cycle_dir: Path) -> Dict[str, Any]:
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


def _load_mutable_targets() -> Dict[str, Any]:
    """
    Best-effort loader for mutable targets health metadata written by mutable_targets_snapshot.py.
    """
    mt_path = Path("runs/memory/mutable_targets.json")
    if not mt_path.exists():
        return {}
    try:
        return json.loads(mt_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def queue_job(job_type: str, params: Dict[str, Any]) -> str:
    """
    Queue a job using ml_scheduler.enqueue_job if available, otherwise stub.
    """
    if enqueue_job and job_type != "code_patch_experiment":
        try:
            job_dict = {
                "job_type": job_type,
                "params": params,
            }
            # Allow caller to pass priority/deadline in params
            if "priority" in params:
                job_dict["priority"] = params["priority"]
            if "deadline" in params:
                job_dict["deadline"] = params["deadline"]

            job_id = enqueue_job(job_dict)
            logger.info("job_enqueued", extra={"job_type": job_type, "params": params, "job_id": job_id})
            return str(job_id)
        except Exception as e:
            logger.warning("job_enqueue_failed", extra={"job_type": job_type, "error": str(e), "params": params})
    # For code_patch_experiment, invoke bridge script directly (best-effort)
    if job_type == "code_patch_experiment":
        try:
            target = params.get("target") or ""
            task_id = params.get("task_id") or params.get("experiment_id") or ""
            patch_output = params.get("patch_output")
            test_cmd = params.get("test_cmd")
            llm_model = params.get("llm_model")
            dry_run = params.get("dry_run", False)
            reasoning_model = params.get("reasoning_model")

            cmd = [
                sys.executable,
                "scripts/ai_trader/research/run_code_patch_experiment.py",
                "--target",
                target,
            ]
            if task_id:
                cmd.extend(["--task-id", task_id])
            if patch_output:
                cmd.extend(["--patch-output", patch_output])
            if test_cmd:
                cmd.extend(["--test-cmd", test_cmd])
            if llm_model:
                cmd.extend(["--llm-model", llm_model])
            if reasoning_model:
                cmd.extend(["--reasoning-model", reasoning_model])
            if dry_run:
                cmd.append("--dry-run")
            if task_id:
                experiment_api.create_experiment({"experiment_id": task_id, "target": target, "kind": "code_patch_experiment"})
            subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parents[3]))
            job_id = task_id or f"patch-{target}"
            logger.info("code_patch_experiment_dispatched", extra={"job_id": job_id, "cmd": cmd})
            try:
                publish_ml_event("started", "code_patch_experiment", job_id, params=params)
            except Exception:
                pass
            return str(job_id)
        except Exception as e:
            logger.warning("code_patch_experiment_dispatch_failed", extra={"error": str(e), "params": params})
    # stub fallback
    logger.info("queue_job called (stub)", extra={"job_type": job_type, "params": params})
    return f"queued-{job_type}"


ALLOWED_KINDS = {
    "backfill",
    "feature_experiment",
    "hpo",
    "strategy_experiment",
    "patch_experiment",
    "routing_experiment",
    "tool_patch_experiment",
    "data_pipeline_patch_experiment",
    "module_patch_experiment",
}


class ExperimentOrchestrator:
    """Translate agent experiments into concrete jobs."""

    def plan_and_enqueue(self, experiments: List[Dict[str, Any]]) -> List[str]:
        job_ids: List[str] = []
        mutable_targets = _load_mutable_targets()

        for exp in experiments or []:
            kind = exp.get("kind")
            target = exp.get("target")
            params = dict(exp.get("params") or {})
            budget = exp.get("budget") or {}
            target_health = {}
            if target and target in mutable_targets:
                target_health = mutable_targets[target].get("metrics", {}) or {}
                params["target_health"] = target_health
                params["target_kind"] = mutable_targets[target].get("kind")

            if not kind or kind not in ALLOWED_KINDS:
                logger.warning("skip_unknown_experiment_kind", extra={"exp": exp})
                continue

            if kind == "patch_experiment" and self._is_forbidden_target(target):
                logger.warning("skip_forbidden_patch_target", extra={"target": target})
                continue
            # If a target is explicitly unhealthy but recommended to sync/fallback, skip auto patch for now.
            if kind in ("tool_patch_experiment", "data_pipeline_patch_experiment", "module_patch_experiment"):
                recommendation = target_health.get("recommendation")
                if recommendation == "sync_or_fallback":
                    logger.warning(
                        "skip_patch_due_to_health",
                        extra={"target": target, "health": target_health},
                    )
                    continue

            job_type = self._map_kind_to_job_type(kind)
            params.setdefault("target", target)
            # Attach budget hints if provided (max runtime/token cost)
            if budget:
                params["budget"] = budget
            # Preserve original kind for downstream handler (self-programming engine)
            params.setdefault("kind", kind)

            job_id = queue_job(job_type=job_type, params=params)
            job_ids.append(job_id)

        return job_ids

    def _map_kind_to_job_type(self, kind: str) -> str:
        if kind == "backfill":
            return "backfill_bars"
        if kind == "feature_experiment":
            return "feature_experiment"
        if kind == "hpo":
            return "hpo_cli"
        if kind == "strategy_experiment":
            return "strategy_experiment"
        if kind == "patch_experiment":
            return "code_patch_experiment"
        if kind == "routing_experiment":
            return "routing_experiment"
        if kind in ("tool_patch_experiment", "data_pipeline_patch_experiment", "module_patch_experiment"):
            return "code_patch_experiment"
        raise ValueError(f"Unsupported experiment kind: {kind}")

    def _is_forbidden_target(self, target: str | None) -> bool:
        if not target:
            return True
        return target.startswith("kernel:") or target.startswith("execution:") or target.startswith("risk:")

    async def execute_experiment_specs(
        self,
        *,
        cycle_id: str,
        experiment_specs: List[Dict[str, Any]],
        agent_tasks: List[Dict[str, Any]] | None = None,
        agent_plan: Dict[str, Any] | None = None,
        state_snapshot: Dict[str, Any] | None = None,
        policy_snapshot: Dict[str, Any] | None = None,
        top_k: int = 1,
        rerun: bool = False,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """Execute strategy experiment specs and persist a full cycle bundle."""
        if ml_scheduler is None:
            raise RuntimeError("ml_scheduler_unavailable")

        normalized_cycle_id = _normalize_cycle_id(cycle_id)
        cycle_dir = RESEARCH_CYCLES_ROOT / normalized_cycle_id
        cycle_dir.mkdir(parents=True, exist_ok=True)
        agent_reports_dir = cycle_dir / "agent_reports"
        agent_reports_dir.mkdir(parents=True, exist_ok=True)
        experiments_dir = cycle_dir / "experiments"
        experiments_dir.mkdir(parents=True, exist_ok=True)
        task_manifest_path = cycle_dir / "task_manifest.json"
        task_execution_log_path = cycle_dir / "task_execution_log.jsonl"
        task_outcome_index_path = cycle_dir / "task_outcome_index.json"
        task_schedule_plan_path = cycle_dir / "task_schedule_plan.json"
        task_budget_summary_path = cycle_dir / "task_budget_summary.json"
        task_retry_report_path = cycle_dir / "task_retry_report.json"
        cycle_state_store = CycleStateStore(cycle_dir=cycle_dir, cycle_id=normalized_cycle_id)
        cycle_state_path = cycle_state_store.cycle_state_path
        stage_history_path = cycle_state_store.stage_history_path
        rerun_report_path = cycle_dir / "rerun_report.json"
        resume_report_path = cycle_dir / "resume_report.json"

        cycle_state = cycle_state_store.load_or_init()
        if resume and str(cycle_state.get("status") or "") == "completed":
            raise RuntimeError("resume_not_allowed_on_completed_cycle")
        rerun_report: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "rerun": bool(rerun),
            "resume": bool(resume),
            "reused_tasks": 0,
            "created_tasks": 0,
            "reused_experiments": 0,
            "executed_experiments": 0,
        }
        resume_report: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "resume": bool(resume),
            "resumed_from_stage": cycle_state.get("current_stage"),
            "skipped_stages": [],
            "executed_stages": [],
        }

        task_compile_reused = bool(
            resume
            and cycle_state_store.stage_completed(cycle_state, "task_compile")
            and (cycle_dir / "agent_tasks" / "tasks.json").exists()
            and task_manifest_path.exists()
        )
        if task_compile_reused:
            resume_report["skipped_stages"].append("task_compile")
            loaded = load_agent_tasks(cycle_dir)
            agent_tasks = loaded or list(agent_tasks or [])
            rerun_report["reused_tasks"] = len(agent_tasks or [])
            task_manifest = _load_json_safe(task_manifest_path)
        else:
            cycle_state = cycle_state_store.enter_stage(
                cycle_state,
                "task_compile",
                detail={"rerun": bool(rerun), "resume": bool(resume)},
            )
            resume_report["executed_stages"].append("task_compile")
            try:
                if not agent_tasks:
                    loaded = load_agent_tasks(cycle_dir)
                    if loaded:
                        agent_tasks = loaded
                        rerun_report["reused_tasks"] = len(agent_tasks or [])
                    else:
                        # Director compiles explicit tasks first, then execution consumes tasks.
                        policy_payload = _as_dict((agent_plan or {}).get("cycle_policy"))
                        agent_tasks = compile_agent_tasks(
                            cycle_id=normalized_cycle_id,
                            cycle_policy=policy_payload,
                            seed_specs=experiment_specs,
                            input_refs=[
                                str(cycle_dir / "blocked_summary.json"),
                                str(cycle_dir / "quality_gate_breakdown.json"),
                                str(cycle_dir / "next_cycle_recommendations.json"),
                                str(cycle_dir / "promotion_decision.json"),
                                str(cycle_dir / "portfolio_evaluation.json"),
                            ],
                        )
                        rerun_report["created_tasks"] = len(agent_tasks or [])
                        persist_agent_tasks(agent_tasks, cycle_dir=cycle_dir)
                else:
                    persist_agent_tasks(agent_tasks, cycle_dir=cycle_dir)
            except Exception as exc:
                cycle_state = cycle_state_store.fail(
                    cycle_state,
                    failed_stage="task_compile",
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
                raise

        normalized_agent_tasks: List[Dict[str, Any]] = []
        for raw_task in (agent_tasks or []):
            task = dict(raw_task)
            research_intent = str(task.get("research_intent") or "seed")
            budget_cost = _as_dict(task.get("budget_cost"))
            max_retries = _as_int(task.get("max_retries"), 0 if research_intent == "retest_data" else 1)
            retryable = bool(task.get("retryable", research_intent != "retest_data"))
            task["priority"] = _as_int(task.get("priority"), _priority_by_intent(research_intent))
            task["budget_cost"] = {
                "compute_hours": _as_float(budget_cost.get("compute_hours"), 0.2 if str(task.get("task_type") or "") == "analysis" else 1.5),
                "token_cost": _as_float(budget_cost.get("token_cost"), 0.0),
            }
            task["max_runtime_minutes"] = _as_int(task.get("max_runtime_minutes"), 20 if str(task.get("task_type") or "") == "analysis" else 60)
            task["max_variants"] = _as_int(task.get("max_variants"), 1)
            task["max_retries"] = max_retries
            task["retryable"] = retryable
            task["retry_count"] = _as_int(task.get("retry_count"), 0)
            task["failure_policy"] = str(task.get("failure_policy") or ("retry_then_defer" if retryable else "no_retry"))
            task.setdefault("defer_reason", None)
            normalized_agent_tasks.append(task)
        agent_tasks = normalized_agent_tasks
        persist_agent_tasks(agent_tasks, cycle_dir=cycle_dir)

        # hard gate: no task file, no execution
        if not (cycle_dir / "agent_tasks" / "tasks.json").exists():
            raise RuntimeError("missing_agent_tasks_file")

        role_grouped: Dict[str, List[Dict[str, Any]]] = {}
        for t in agent_tasks or []:
            role = str(t.get("agent_role") or "unknown")
            role_grouped.setdefault(role, []).append(dict(t))
        if not task_compile_reused:
            task_manifest = {
                "cycle_id": normalized_cycle_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "task_count": len(agent_tasks or []),
                "tasks": [dict(t) for t in (agent_tasks or [])],
                "tasks_by_role": role_grouped,
            }
            task_manifest_path.write_text(json.dumps(task_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            task_execution_log_path.write_text("", encoding="utf-8")
            cycle_state = cycle_state_store.complete_stage(
                cycle_state,
                "task_compile",
                detail={"task_count": len(agent_tasks or [])},
            )

        normalized_specs: List[Dict[str, Any]] = []
        specs_by_experiment_id: Dict[str, Dict[str, Any]] = {}
        candidate_results: List[Dict[str, Any]] = []
        execution_reports: List[Dict[str, Any]] = []
        task_outcome_index: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "outcomes": []}
        retry_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "tasks": []}
        task_by_id = {str(t.get("task_id")): dict(t) for t in (agent_tasks or []) if str(t.get("task_id") or "")}
        strategy_tasks = [dict(t) for t in (agent_tasks or []) if str(t.get("task_type") or "").strip().lower() == "strategy_experiment"]

        def _append_task_event(event: Dict[str, Any]) -> None:
            with task_execution_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

        execute_reused = bool(
            resume
            and cycle_state_store.stage_completed(cycle_state, "execute")
            and (cycle_dir / "cycle_spec.json").exists()
            and experiments_dir.exists()
        )
        if execute_reused:
            resume_report["skipped_stages"].append("execute")
            cycle_spec_loaded = _load_json_safe(cycle_dir / "cycle_spec.json")
            normalized_specs = [dict(x) for x in cycle_spec_loaded.get("experiment_specs", []) if isinstance(x, dict)]
            for spec in normalized_specs:
                exp_id = str(spec.get("experiment_id") or "")
                if exp_id:
                    specs_by_experiment_id[exp_id] = spec
                    exp_payload = _load_json_safe(experiments_dir / f"{exp_id}.json")
                    if exp_payload:
                        candidate_results.append(exp_payload)
                        rerun_report["reused_experiments"] = int(rerun_report["reused_experiments"]) + 1
            loaded_outcomes = _load_json_safe(task_outcome_index_path)
            if loaded_outcomes:
                task_outcome_index = loaded_outcomes
            loaded_retry = _load_json_safe(task_retry_report_path)
            if loaded_retry:
                retry_report = loaded_retry
            loaded_exec = _load_json_safe(agent_reports_dir / "execution_reports.json")
            if isinstance(loaded_exec, dict):
                execution_reports = []
        else:
            cycle_state = cycle_state_store.enter_stage(
                cycle_state,
                "execute",
                detail={"scheduled_tasks": len(strategy_tasks)},
            )
            resume_report["executed_stages"].append("execute")

        task_defaults: List[Dict[str, Any]] = []
        for task in strategy_tasks:
            research_intent = str(task.get("research_intent") or "seed")
            budget_cost = _as_dict(task.get("budget_cost"))
            max_runtime_minutes = _as_int(task.get("max_runtime_minutes"), 60)
            max_variants = _as_int(task.get("max_variants"), 1)
            max_retries = _as_int(task.get("max_retries"), 1)
            retryable = bool(task.get("retryable", True))
            retry_count = _as_int(task.get("retry_count"), 0)
            failure_policy = str(task.get("failure_policy") or ("retry_then_defer" if retryable else "no_retry"))
            priority = _as_int(task.get("priority"), _priority_by_intent(research_intent))
            task_defaults.append(
                {
                    **dict(task),
                    "research_intent": research_intent,
                    "priority": priority,
                    "budget_cost": {
                        "compute_hours": _as_float(budget_cost.get("compute_hours"), 1.0),
                        "token_cost": _as_float(budget_cost.get("token_cost"), 0.0),
                    },
                    "max_runtime_minutes": max_runtime_minutes,
                    "max_variants": max_variants,
                    "max_retries": max_retries,
                    "retryable": retryable,
                    "retry_count": retry_count,
                    "failure_policy": failure_policy,
                    "defer_reason": task.get("defer_reason"),
                }
            )

        ordered_tasks = sorted(
            task_defaults,
            key=lambda t: (-_as_int(t.get("priority"), 0), str(t.get("task_id") or "")),
        )
        budget_cfg = _as_dict((agent_plan or {}).get("task_budget_limits"))
        max_cycle_compute_hours = _as_float(budget_cfg.get("max_cycle_compute_hours"), 3.0)
        max_cycle_runtime_minutes = _as_int(budget_cfg.get("max_cycle_runtime_minutes"), 180)
        max_scheduled_tasks = _as_int(budget_cfg.get("max_scheduled_tasks"), max(1, len(ordered_tasks) or 1))

        if execute_reused:
            task_schedule_plan = _load_json_safe(task_schedule_plan_path)
            task_budget_summary = _load_json_safe(task_budget_summary_path)
            scheduled_tasks = []
            deferred_tasks = []
            dropped_tasks = []
        else:
            running_compute = 0.0
            running_runtime = 0
            scheduled_tasks: List[Dict[str, Any]] = []
            deferred_tasks: List[Dict[str, Any]] = []
            dropped_tasks: List[Dict[str, Any]] = []

            schedule_entries: List[Dict[str, Any]] = []
            for task in ordered_tasks:
                task_id = str(task.get("task_id") or "")
                blocked_by = _as_dict(task.get("blocked_by"))
                compute_hours = _as_float(_as_dict(task.get("budget_cost")).get("compute_hours"), 1.0)
                runtime_minutes = _as_int(task.get("max_runtime_minutes"), 60)
                reason = None
                decision = "scheduled"
                if blocked_by.get("type") == "policy_suppression":
                    decision = "dropped"
                    reason = "policy_suppression"
                    task["defer_reason"] = reason
                    dropped_tasks.append(task)
                elif len(scheduled_tasks) >= max_scheduled_tasks:
                    decision = "deferred"
                    reason = "max_scheduled_tasks_exceeded"
                    task["defer_reason"] = reason
                    deferred_tasks.append(task)
                elif (running_compute + compute_hours) > max_cycle_compute_hours:
                    decision = "deferred"
                    reason = "cycle_compute_budget_exceeded"
                    task["defer_reason"] = reason
                    deferred_tasks.append(task)
                elif (running_runtime + runtime_minutes) > max_cycle_runtime_minutes:
                    decision = "deferred"
                    reason = "cycle_runtime_budget_exceeded"
                    task["defer_reason"] = reason
                    deferred_tasks.append(task)
                else:
                    running_compute += compute_hours
                    running_runtime += runtime_minutes
                    scheduled_tasks.append(task)
                schedule_entries.append(
                    {
                        "task_id": task_id,
                        "research_intent": task.get("research_intent"),
                        "priority": task.get("priority"),
                        "decision": decision,
                        "reason": reason,
                        "budget_cost": task.get("budget_cost"),
                        "max_runtime_minutes": task.get("max_runtime_minutes"),
                    }
                )

            task_schedule_plan = {
                "cycle_id": normalized_cycle_id,
                "limits": {
                    "max_cycle_compute_hours": max_cycle_compute_hours,
                    "max_cycle_runtime_minutes": max_cycle_runtime_minutes,
                    "max_scheduled_tasks": max_scheduled_tasks,
                },
                "ordered_candidates": [str(t.get("task_id") or "") for t in ordered_tasks],
                "scheduled": [str(t.get("task_id") or "") for t in scheduled_tasks],
                "deferred": [str(t.get("task_id") or "") for t in deferred_tasks],
                "dropped": [str(t.get("task_id") or "") for t in dropped_tasks],
                "entries": schedule_entries,
                "executed_order": [],
            }
            task_schedule_plan_path.write_text(json.dumps(task_schedule_plan, indent=2, ensure_ascii=False), encoding="utf-8")
            task_budget_summary = {
                "cycle_id": normalized_cycle_id,
                "limits": task_schedule_plan["limits"],
                "used": {
                    "compute_hours": running_compute,
                    "runtime_minutes": running_runtime,
                },
                "scheduled_count": len(scheduled_tasks),
                "deferred_count": len(deferred_tasks),
                "dropped_count": len(dropped_tasks),
                "deferred_task_ids": task_schedule_plan["deferred"],
                "dropped_task_ids": task_schedule_plan["dropped"],
            }
            task_budget_summary_path.write_text(json.dumps(task_budget_summary, indent=2, ensure_ascii=False), encoding="utf-8")

            for task in deferred_tasks + dropped_tasks:
                task_id = str(task.get("task_id") or "")
                task_outcome_index["outcomes"].append(
                    {
                        "task_id": task_id,
                        "experiment_id": None,
                        "status": "deferred" if task in deferred_tasks else "dropped",
                        "derived_from": task.get("derived_from") or _as_dict(task_by_id.get(task_id, {}).get("derived_from")),
                        "defer_reason": task.get("defer_reason"),
                    }
                )
                _append_task_event(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "event": "task_deferred" if task in deferred_tasks else "task_dropped",
                        "cycle_id": normalized_cycle_id,
                        "task_id": task_id,
                        "status": "deferred" if task in deferred_tasks else "dropped",
                        "reason": task.get("defer_reason"),
                    }
                )

        def _task_to_spec(task: Dict[str, Any]) -> Dict[str, Any]:
            patch = _as_dict(task.get("task_patch"))
            market = str(patch.get("market") or "stocks").strip().lower()
            spec = dict(patch)
            spec["cycle_id"] = normalized_cycle_id
            spec["market"] = market or "stocks"
            spec["research_intent"] = task.get("research_intent") or "seed"
            spec["task_id"] = task.get("task_id")
            spec["derived_from"] = task.get("derived_from") or {}
            if task.get("blocked_by"):
                spec["blocked_by"] = task.get("blocked_by")
            if not isinstance(spec.get("engine"), dict):
                spec["engine"] = {
                    "adapter": "stocks_kernel_backtest" if market == "stocks" else "options_single_leg_replay",
                    "mode": "backtest" if market == "stocks" else "replay",
                }
            if not spec.get("strategy_family"):
                spec["strategy_family"] = f"{spec['market']}_scalp"
            spec["experiment_id"] = str(spec.get("experiment_id") or experiment_api.stable_experiment_id(spec))
            return spec

        for idx, task in enumerate(scheduled_tasks, start=1):
            spec = _task_to_spec(task)
            task_id = str(spec.get("task_id") or "")
            experiment_id = str(spec.get("experiment_id") or experiment_api.stable_experiment_id(spec))
            spec["experiment_id"] = experiment_id
            normalized_specs.append(spec)
            specs_by_experiment_id[experiment_id] = spec
            task_schedule_plan["executed_order"].append(task_id)
            _append_task_event(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": "task_started",
                    "cycle_id": normalized_cycle_id,
                    "task_id": task_id,
                    "experiment_id": experiment_id,
                    "priority": task.get("priority"),
                }
            )

            existing_result = _load_json_safe(experiments_dir / f"{experiment_id}.json")
            existing_status = str(existing_result.get("status") or "").strip().lower()
            if existing_result and existing_status in {"completed", "blocked", "failed", "error"}:
                rerun_report["reused_experiments"] = int(rerun_report["reused_experiments"]) + 1
                existing_result["task_id"] = task_id
                if not existing_result.get("derived_from"):
                    existing_result["derived_from"] = spec.get("derived_from") or _as_dict(task_by_id.get(task_id, {}).get("derived_from"))
                candidate_results.append(existing_result)
                task_outcome_index["outcomes"].append(
                    {
                        "task_id": task_id,
                        "experiment_id": experiment_id,
                        "status": existing_result.get("status"),
                        "derived_from": existing_result.get("derived_from"),
                        "defer_reason": _as_dict(existing_result).get("defer_reason"),
                    }
                )
                _append_task_event(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "event": "task_completed",
                        "cycle_id": normalized_cycle_id,
                        "task_id": task_id,
                        "experiment_id": experiment_id,
                        "status": existing_result.get("status"),
                        "reused": True,
                    }
                )
                continue

            blocked_by = _as_dict(spec.get("blocked_by"))
            if blocked_by.get("type") == "data_readiness":
                blocked_result = {
                    "experiment_id": experiment_id,
                    "task_id": task_id,
                    "derived_from": spec.get("derived_from") or _as_dict(task_by_id.get(task_id, {}).get("derived_from")),
                    "cycle_id": normalized_cycle_id,
                    "status": "blocked",
                    "market": spec.get("market"),
                    "universe": spec.get("universe") or {},
                    "strategy_family": spec.get("strategy_family"),
                    "summary_metrics": {},
                    "quality_flags": {"data_ok": False, "enough_trades": False, "passed_acceptance": False},
                    "robustness": {
                        "ok": False,
                        "status": "fail",
                        "failure_type": "data_insufficient",
                        "next_action": blocked_by.get("next_action") or "widen_window_or_drop_symbol",
                        "issues": ["data_readiness_blocked"],
                    },
                    "execution_reality": spec.get("execution_reality") or {},
                    "sizing": spec.get("sizing") or {},
                    "lineage": spec.get("lineage") or {},
                    "blocked_by": blocked_by,
                    "notes": [f"data_readiness_blocked:{blocked_by.get('reason') or 'unknown'}"],
                    "artifacts": {},
                }
                execution_reports.append(
                    {
                        "job_id": f"{normalized_cycle_id}-exp-{idx:02d}",
                        "task_id": task_id,
                        "experiment_id": experiment_id,
                        "ok": False,
                        "status": "blocked",
                        "error": "data_readiness_blocked",
                    }
                )
                retry_report["tasks"].append(
                    {
                        "task_id": task_id,
                        "experiment_id": experiment_id,
                        "retryable": False,
                        "retry_count": 0,
                        "max_retries": _as_int(task.get("max_retries"), 0),
                        "final_status": "deferred",
                        "failure_policy": "no_retry_data_block",
                        "defer_reason": "data_readiness_blocked",
                        "action": "deferred",
                    }
                )
                candidate_results.append(blocked_result)
                (experiments_dir / f"{experiment_id}.json").write_text(
                    json.dumps(blocked_result, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                task_outcome_index["outcomes"].append(
                    {
                        "task_id": task_id,
                        "experiment_id": experiment_id,
                        "status": "blocked",
                        "derived_from": blocked_result.get("derived_from"),
                        "defer_reason": "data_readiness_blocked",
                    }
                )
                _append_task_event(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "event": "task_completed",
                        "cycle_id": normalized_cycle_id,
                        "task_id": task_id,
                        "experiment_id": experiment_id,
                        "status": "blocked",
                    }
                )
                continue

            max_retries = _as_int(task.get("max_retries"), 0)
            retryable = bool(task.get("retryable", True))
            failure_policy = str(task.get("failure_policy") or "retry_then_defer")
            max_attempts = 1 + (max_retries if retryable else 0)
            attempts = 0
            run_result: Dict[str, Any] = {}
            last_error = ""
            while attempts < max_attempts:
                attempts += 1
                job = {
                    "job_id": f"{normalized_cycle_id}-exp-{idx:02d}-try-{attempts}",
                    "job_type": "strategy_experiment",
                    "params": spec,
                }
                try:
                    run_result = await ml_scheduler.execute_job(job)
                except Exception as exc:
                    cycle_state = cycle_state_store.fail(
                        cycle_state,
                        failed_stage="execute",
                        error_summary=f"{type(exc).__name__}: {exc}",
                    )
                    raise
                ok = bool(run_result.get("ok", False))
                status = str(run_result.get("status") or "")
                last_error = str(run_result.get("error") or "")
                if ok or status == "completed":
                    break
                error_text = last_error.lower()
                temporary_error = any(token in error_text for token in ("timeout", "tempor", "502", "429", "connection", "unavailable"))
                if not (retryable and temporary_error and attempts < max_attempts):
                    break
            rerun_report["executed_experiments"] = int(rerun_report["executed_experiments"]) + 1

            execution_reports.append(
                {
                    "job_id": f"{normalized_cycle_id}-exp-{idx:02d}",
                    "task_id": task_id,
                    "experiment_id": experiment_id,
                    "ok": bool(run_result.get("ok", False)),
                    "status": run_result.get("status"),
                    "error": run_result.get("error"),
                    "attempts": attempts,
                }
            )

            experiment_result = run_result.get("experiment_result")
            if not isinstance(experiment_result, dict):
                loaded = experiment_api.load_experiment(experiment_id, normalized_cycle_id)
                experiment_result = loaded.get("result") if isinstance(loaded, dict) else {}
            if not isinstance(experiment_result, dict):
                experiment_result = {
                    "experiment_id": experiment_id,
                    "cycle_id": normalized_cycle_id,
                    "status": "failed",
                    "errors": ["missing_experiment_result"],
                }
            experiment_result["task_id"] = task_id
            experiment_result["derived_from"] = spec.get("derived_from") or _as_dict(task_by_id.get(task_id, {}).get("derived_from"))
            experiment_result.setdefault("market", spec.get("market"))
            experiment_result.setdefault("universe", spec.get("universe") or {})
            experiment_result.setdefault("strategy_family", spec.get("strategy_family"))

            status = str(experiment_result.get("status") or run_result.get("status") or "")
            defer_reason = None
            action = "none"
            if status in {"failed", "error"}:
                action = "deferred" if retryable else "no_retry"
                defer_reason = "retry_exhausted" if retryable else "non_retryable_failure"
            elif status == "blocked":
                robustness = _as_dict(experiment_result.get("robustness"))
                failure_type = str(robustness.get("failure_type") or "")
                if failure_type in {"threshold_not_met", "walk_forward_failed", "robustness_failed"}:
                    action = "deferred_to_retest"
                    defer_reason = "robustness_retest"
            retry_report["tasks"].append(
                {
                    "task_id": task_id,
                    "experiment_id": experiment_id,
                    "retryable": retryable,
                    "retry_count": max(0, attempts - 1),
                    "max_retries": max_retries,
                    "final_status": status or str(run_result.get("status") or "unknown"),
                    "failure_policy": failure_policy,
                    "defer_reason": defer_reason,
                    "last_error": last_error or None,
                    "action": action,
                }
            )

            candidate_results.append(experiment_result)
            (experiments_dir / f"{experiment_id}.json").write_text(
                json.dumps(experiment_result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            task_outcome_index["outcomes"].append(
                {
                    "task_id": task_id,
                    "experiment_id": experiment_id,
                    "status": experiment_result.get("status"),
                    "derived_from": experiment_result.get("derived_from"),
                    "defer_reason": defer_reason,
                }
            )
            _append_task_event(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": "task_completed",
                    "cycle_id": normalized_cycle_id,
                    "task_id": task_id,
                    "experiment_id": experiment_id,
                    "status": experiment_result.get("status"),
                    "attempts": attempts,
                }
            )

        task_schedule_plan_path.write_text(json.dumps(task_schedule_plan, indent=2, ensure_ascii=False), encoding="utf-8")
        task_retry_report_path.write_text(json.dumps(retry_report, indent=2, ensure_ascii=False), encoding="utf-8")
        if not execute_reused:
            cycle_state = cycle_state_store.complete_stage(
                cycle_state,
                "execute",
                detail={
                    "experiment_count": len(normalized_specs),
                    "executed_experiments": rerun_report.get("executed_experiments"),
                    "reused_experiments": rerun_report.get("reused_experiments"),
                },
            )

        cycle_spec = {
            "cycle_id": normalized_cycle_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "experiment_count": len(normalized_specs),
            "experiment_specs": normalized_specs,
        }
        (cycle_dir / "cycle_spec.json").write_text(
            json.dumps(cycle_spec, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        _write_agent_reports(
            agent_reports_dir=agent_reports_dir,
            agent_plan=agent_plan,
            state_snapshot=state_snapshot,
            policy_snapshot=policy_snapshot,
            execution_reports=execution_reports,
        )
        task_outcome_index_path.write_text(json.dumps(task_outcome_index, indent=2, ensure_ascii=False), encoding="utf-8")

        evaluation_bundle_path = cycle_dir / "evaluation_bundle.json"
        evaluate_reused = bool(
            resume
            and cycle_state_store.stage_completed(cycle_state, "evaluate")
            and evaluation_bundle_path.exists()
        )
        if evaluate_reused:
            resume_report["skipped_stages"].append("evaluate")
            evaluation = _load_json_safe(evaluation_bundle_path)
            evaluation["artifacts"] = {
                "output_dir": str(cycle_dir),
                "leaderboard_json": str(cycle_dir / "leaderboard.json"),
                "promotion_decision_json": str(cycle_dir / "promotion_decision.json"),
                "portfolio_evaluation_json": str(cycle_dir / "portfolio_evaluation.json"),
                "blocked_summary_json": str(cycle_dir / "blocked_summary.json"),
                "quality_gate_breakdown_json": str(cycle_dir / "quality_gate_breakdown.json"),
                "next_cycle_recommendations_json": str(cycle_dir / "next_cycle_recommendations.json"),
                "evaluation_bundle_json": str(evaluation_bundle_path),
            }
        else:
            cycle_state = cycle_state_store.enter_stage(cycle_state, "evaluate", detail={})
            resume_report["executed_stages"].append("evaluate")
            try:
                evaluation = persist_evaluation_bundle(
                    candidate_results,
                    output_dir=cycle_dir,
                    specs_by_experiment_id=specs_by_experiment_id,
                    cycle_id=normalized_cycle_id,
                    top_k=top_k,
                )
            except Exception as exc:
                cycle_state = cycle_state_store.fail(
                    cycle_state,
                    failed_stage="evaluate",
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
                raise
            cycle_state = cycle_state_store.complete_stage(
                cycle_state,
                "evaluate",
                detail={"leaderboard_count": len(evaluation.get("leaderboard", []))},
            )

        policy_reused = bool(
            resume
            and cycle_state_store.stage_completed(cycle_state, "policy_compile")
            and (cycle_dir / "next_cycle_policy.json").exists()
        )
        if policy_reused:
            resume_report["skipped_stages"].append("policy_compile")
            policy_payload = _load_json_safe(cycle_dir / "next_cycle_policy.json")
            cycle_policy = {
                "policy": policy_payload,
                "path": str(cycle_dir / "next_cycle_policy.json"),
                "feedback_mapping_report_path": str(cycle_dir / "feedback_mapping_report.json"),
            }
        else:
            cycle_state = cycle_state_store.enter_stage(cycle_state, "policy_compile", detail={})
            resume_report["executed_stages"].append("policy_compile")
            try:
                cycle_policy = persist_next_cycle_policy(
                    evaluation,
                    output_dir=cycle_dir,
                    cycle_id=normalized_cycle_id,
                    task_outcome_index=task_outcome_index,
                    task_schedule_plan=task_schedule_plan,
                    task_retry_report=retry_report,
                    task_manifest=task_manifest,
                )
            except Exception as exc:
                cycle_state = cycle_state_store.fail(
                    cycle_state,
                    failed_stage="policy_compile",
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
                raise
            cycle_state = cycle_state_store.complete_stage(cycle_state, "policy_compile", detail={})

        candidate_pool_artifacts = update_candidate_pool_from_evaluation(
            cycle_id=normalized_cycle_id,
            evaluation_bundle=evaluation,
        )
        candidate_pool_payload = _load_json_safe(Path(candidate_pool_artifacts["candidate_pool_json"]))
        portfolio_selection = build_portfolio_selection(
            evaluation_bundle=evaluation,
            candidate_pool=candidate_pool_payload,
            max_selected=2,
        )
        portfolio_selection_path = persist_portfolio_selection(portfolio_selection, output_dir=cycle_dir)
        allocation_plan = build_allocation_plan(
            portfolio_selection=portfolio_selection,
            risk_budget_bps_total=100.0,
            capital_total=100_000.0,
        )
        allocation_plan_path = persist_allocation_plan(allocation_plan, output_dir=cycle_dir)
        promotion_gate = build_promotion_gate(
            candidate_results=candidate_results,
            portfolio_selection=portfolio_selection,
        )
        promotion_gate_path = persist_promotion_gate(promotion_gate, output_dir=cycle_dir)
        shadow_bundle = build_shadow_candidates(
            promotion_gate=promotion_gate,
            allocation_plan=allocation_plan,
            candidate_results=candidate_results,
            output_dir=cycle_dir,
        )
        paper_bridge = build_paper_candidates(
            shadow_candidates_dir=shadow_bundle["shadow_candidates_dir"],
            output_dir=cycle_dir,
        )
        live_candidates_dir = str(cycle_dir / "live_candidates")
        live_runs_dir = str(cycle_dir / "live_runs")
        state_root = Path("runs/ai_trader/research/state")
        operator_controls = load_operator_controls(state_root=state_root, create_if_missing=True)
        operator_controls_path = state_root / "operator_controls.json"
        risk_guard_report_path = cycle_dir / "risk_guard_report.json"
        capital_governor_report_path = cycle_dir / "capital_governor_report.json"
        session_control_report_path = cycle_dir / "session_control_report.json"
        operator_audit_log_path = cycle_dir / "operator_audit_log.jsonl"
        control_decision_index_path = cycle_dir / "control_decision_index.json"
        broker_session_path = cycle_dir / "broker_session.json"
        orders_state_path = cycle_dir / "orders_state.json"
        orders_events_path = cycle_dir / "orders_events.jsonl"
        account_snapshot_path = cycle_dir / "account_snapshot.json"
        positions_snapshot_path = cycle_dir / "positions_snapshot.json"
        position_reconcile_report_path = cycle_dir / "position_reconcile_report.json"
        live_risk_report_path = cycle_dir / "live_risk_report.json"
        execution_reconcile_report_path = cycle_dir / "execution_reconcile_report.json"
        kill_switch_report_path = cycle_dir / "kill_switch_report.json"
        recovery_report_path = cycle_dir / "recovery_report.json"
        broker_profile_report_path = cycle_dir / "broker_profile_report.json"
        broker_binding_report_path = cycle_dir / "broker_binding_report.json"
        real_broker_dry_run_path = cycle_dir / "real_broker_dry_run.json"
        broker_safety_gate_path = cycle_dir / "broker_safety_gate.json"
        broker_launch_switch_path = cycle_dir / "broker_launch_switch.json"
        broker_readonly_probe_path = cycle_dir / "broker_readonly_probe.json"
        broker_sandbox_orders_path = cycle_dir / "broker_sandbox_orders.json"
        broker_sandbox_events_path = cycle_dir / "broker_sandbox_events.jsonl"
        broker_reconcile_report_path = cycle_dir / "broker_reconcile_report.json"
        eod_closeout_report_path = cycle_dir / "eod_closeout_report.json"
        broker_recovery_report_path = cycle_dir / "broker_recovery_report.json"
        broker_launch_barrier_path = cycle_dir / "broker_launch_barrier.json"
        canary_launch_gate_path = cycle_dir / "canary_launch_gate.json"
        manual_approval_report_path = cycle_dir / "manual_approval_report.json"
        intraday_monitor_report_path = cycle_dir / "intraday_monitor_report.json"
        post_trade_attribution_path = cycle_dir / "post_trade_attribution.json"
        canary_feedback_report_path = cycle_dir / "canary_feedback_report.json"
        live_batch_plan_path = cycle_dir / "live_batch_plan.json"
        live_batch_status_path = cycle_dir / "live_batch_status.json"
        pre_open_prep_report_path = cycle_dir / "pre_open_prep_report.json"
        intraday_batch_report_path = cycle_dir / "intraday_batch_report.json"
        eod_batch_close_report_path = cycle_dir / "eod_batch_close_report.json"
        live_ops_summary_path = cycle_dir / "live_ops_summary.json"
        live_batch_feedback_report_path = cycle_dir / "live_batch_feedback_report.json"
        trading_day_status_path = cycle_dir / "trading_day_status.json"
        ops_control_center_path = cycle_dir / "ops_control_center.json"
        end_of_day_review_path = cycle_dir / "end_of_day_review.json"
        weekly_candidate_review_path = cycle_dir / "weekly_candidate_review.json"
        review_plan_patch_path = cycle_dir / "review_plan_patch.json"
        human_ops_summary_path = cycle_dir / "human_ops_summary.json"
        multi_day_run_plan_path = cycle_dir / "multi_day_run_plan.json"
        multi_day_run_report_path = cycle_dir / "multi_day_run_report.json"
        incident_log_path = cycle_dir / "incident_log.jsonl"
        incident_index_path = cycle_dir / "incident_index.json"
        recovery_coordinator_report_path = cycle_dir / "recovery_coordinator_report.json"
        operator_shift_summary_path = cycle_dir / "operator_shift_summary.json"
        reliability_metrics_path = cycle_dir / "reliability_metrics.json"
        ops_registry_path = Path("runs/ai_trader/research/state/ops_registry.json")
        live_capability_inventory_path = cycle_dir / "live_capability_inventory.json"
        live_reuse_plan_path = cycle_dir / "live_reuse_plan.json"
        live_integration_report_path = cycle_dir / "live_integration_report.json"
        live_gap_list_path = cycle_dir / "live_gap_list.json"
        live_feedback_artifacts = {
            "live_feedback_json": str(cycle_dir / "live_feedback.json"),
            "live_feedback_index_json": str(Path("runs/ai_trader/research/state") / "live_feedback_index.json"),
        }
        live_execution: Dict[str, Any] = {"live_runs_dir": live_runs_dir, "items": []}
        live_feedback: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "items": [], "policy_mapped_feedback": []}
        live_bridge: Dict[str, Any] = {"live_candidates_dir": live_candidates_dir, "live_manifests": []}
        broker_adapter: Any | None = None
        broker_pipeline: Dict[str, Any] = {
            "broker_session_json": str(broker_session_path),
            "broker_profile_report_json": str(broker_profile_report_path),
            "broker_binding_report_json": str(broker_binding_report_path),
            "real_broker_dry_run_json": str(real_broker_dry_run_path),
            "broker_safety_gate_json": str(broker_safety_gate_path),
            "broker_launch_switch_json": str(broker_launch_switch_path),
            "broker_readonly_probe_json": str(broker_readonly_probe_path),
            "broker_sandbox_orders_json": str(broker_sandbox_orders_path),
            "broker_sandbox_events_jsonl": str(broker_sandbox_events_path),
            "broker_reconcile_report_json": str(broker_reconcile_report_path),
            "eod_closeout_report_json": str(eod_closeout_report_path),
            "broker_recovery_report_json": str(broker_recovery_report_path),
            "broker_launch_barrier_json": str(broker_launch_barrier_path),
            "canary_launch_gate_json": str(canary_launch_gate_path),
            "manual_approval_report_json": str(manual_approval_report_path),
            "canary_run_summary_json": str(cycle_dir / "canary_runs" / "unknown" / "canary_run_summary.json"),
            "intraday_monitor_report_json": str(intraday_monitor_report_path),
            "post_trade_attribution_json": str(post_trade_attribution_path),
            "canary_feedback_report_json": str(canary_feedback_report_path),
            "live_batch_plan_json": str(live_batch_plan_path),
            "live_batch_status_json": str(live_batch_status_path),
            "pre_open_prep_report_json": str(pre_open_prep_report_path),
            "intraday_batch_report_json": str(intraday_batch_report_path),
            "eod_batch_close_report_json": str(eod_batch_close_report_path),
            "live_ops_summary_json": str(live_ops_summary_path),
            "live_batch_feedback_report_json": str(live_batch_feedback_report_path),
            "orders_state_json": str(orders_state_path),
            "orders_events_jsonl": str(orders_events_path),
            "account_snapshot_json": str(account_snapshot_path),
            "positions_snapshot_json": str(positions_snapshot_path),
            "position_reconcile_report_json": str(position_reconcile_report_path),
            "live_risk_report_json": str(live_risk_report_path),
            "execution_reconcile_report_json": str(execution_reconcile_report_path),
            "kill_switch_report_json": str(kill_switch_report_path),
            "recovery_report_json": str(recovery_report_path),
        }
        reuse_audit_paths: Dict[str, str] = {
            "live_capability_inventory_json": str(live_capability_inventory_path),
            "live_reuse_plan_json": str(live_reuse_plan_path),
            "live_integration_report_json": str(live_integration_report_path),
            "live_gap_list_json": str(live_gap_list_path),
        }
        try:
            live_bridge = build_live_candidates(
                paper_candidates_dir=paper_bridge["paper_candidates_dir"],
                output_dir=cycle_dir,
                default_broker_profile="paper",
                operator_state_root=state_root,
            )
            live_feedback_index_payload = _load_json_safe(Path(live_feedback_artifacts["live_feedback_index_json"]))
            capital_governor_report = evaluate_capital_governor(
                cycle_id=normalized_cycle_id,
                live_candidates_dir=live_bridge["live_candidates_dir"],
                operator_controls=operator_controls,
                live_feedback_index=live_feedback_index_payload,
            )
            capital_governor_report_path.write_text(
                json.dumps(capital_governor_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            paper_manifests = paper_bridge.get("paper_manifests") or []
            simulate_kill = (
                str(next(iter(paper_manifests), "")).split("/")[-2] if "fixture" in normalized_cycle_id else None
            )
            risk_guard_report = evaluate_live_risk_guard(
                cycle_id=normalized_cycle_id,
                live_candidates_dir=live_bridge["live_candidates_dir"],
                single_trade_max_risk_bps=25.0,
                daily_max_drawdown_bps=80.0,
                max_position_notional=75_000.0,
                blacklist_symbols=[],
                cooldown_candidates=[],
                simulate_kill_switch_candidate=simulate_kill,
                operator_state_root=state_root,
            )
            risk_guard_report_path.write_text(
                json.dumps(risk_guard_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            session_control_report = evaluate_session_control(
                cycle_id=normalized_cycle_id,
                live_candidates_dir=live_bridge["live_candidates_dir"],
                operator_controls=operator_controls,
                risk_guard_report=risk_guard_report,
                simulate_pre_close_flatten=("fixture" in normalized_cycle_id),
            )
            session_control_report_path.write_text(
                json.dumps(session_control_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_execution = run_live_executor(
                cycle_id=normalized_cycle_id,
                live_candidates_dir=live_bridge["live_candidates_dir"],
                risk_guard_report=risk_guard_report,
                capital_governor_report=capital_governor_report,
                session_control_report=session_control_report,
                output_dir=cycle_dir,
                operator_state_root=state_root,
            )
            oms = run_order_manager(
                cycle_id=normalized_cycle_id,
                live_candidates_dir=live_bridge["live_candidates_dir"],
                risk_guard_report=risk_guard_report,
                output_dir=cycle_dir,
                broker_profile=str(os.getenv("AI_TRADER_BROKER_PROFILE", "paper")),
                operator_state_root=state_root,
            )
            broker_adapter = oms.get("broker_adapter")
            account_sync = sync_account_snapshot(
                cycle_id=normalized_cycle_id,
                broker_snapshot=_as_dict(oms.get("broker_snapshot")),
                output_dir=cycle_dir,
            )
            pos_sync = sync_positions_snapshot(
                cycle_id=normalized_cycle_id,
                broker_snapshot=_as_dict(oms.get("broker_snapshot")),
                live_candidates_dir=live_bridge["live_candidates_dir"],
                output_dir=cycle_dir,
            )
            live_risk = enforce_live_risk(
                cycle_id=normalized_cycle_id,
                orders_state=_as_dict(oms.get("orders_state")),
                positions_snapshot=_as_dict(pos_sync.get("positions_snapshot")),
                output_dir=cycle_dir,
            )
            exec_reconcile = run_execution_reconcile(
                cycle_id=normalized_cycle_id,
                orders_state=_as_dict(oms.get("orders_state")),
                broker_snapshot=_as_dict(oms.get("broker_snapshot")),
                position_reconcile_report=_as_dict(pos_sync.get("position_reconcile_report")),
                output_dir=cycle_dir,
            )
            kill = run_live_kill_switch(
                cycle_id=normalized_cycle_id,
                operator_controls=operator_controls,
                live_risk_report=_as_dict(live_risk.get("live_risk_report")),
                positions_snapshot=_as_dict(pos_sync.get("positions_snapshot")),
                output_dir=cycle_dir,
            )
            recovery = run_recovery_playbook(
                cycle_id=normalized_cycle_id,
                broker_session=_load_json_safe(_as_dict(oms).get("broker_session_json", "")),
                account_snapshot=_as_dict(account_sync.get("account_snapshot")),
                positions_snapshot=_as_dict(pos_sync.get("positions_snapshot")),
                orders_state=_as_dict(oms.get("orders_state")),
                kill_switch_report=_as_dict(kill.get("kill_switch_report")),
                output_dir=cycle_dir,
            )
            broker_pipeline = {
                "broker_session_json": str(_as_dict(oms).get("broker_session_json") or broker_session_path),
                "broker_profile_report_json": str(_as_dict(oms).get("broker_profile_report_json") or broker_profile_report_path),
                "broker_binding_report_json": str(_as_dict(oms).get("broker_binding_report_json") or broker_binding_report_path),
                "real_broker_dry_run_json": str(_as_dict(oms).get("real_broker_dry_run_json") or real_broker_dry_run_path),
                "broker_safety_gate_json": str(broker_safety_gate_path),
                "broker_launch_switch_json": str(_as_dict(oms).get("broker_launch_switch_json") or broker_launch_switch_path),
                "orders_state_json": str(_as_dict(oms).get("orders_state_json") or orders_state_path),
                "orders_events_jsonl": str(_as_dict(oms).get("orders_events_jsonl") or orders_events_path),
                "account_snapshot_json": str(_as_dict(account_sync).get("account_snapshot_json") or account_snapshot_path),
                "positions_snapshot_json": str(_as_dict(pos_sync).get("positions_snapshot_json") or positions_snapshot_path),
                "position_reconcile_report_json": str(_as_dict(pos_sync).get("position_reconcile_report_json") or position_reconcile_report_path),
                "live_risk_report_json": str(_as_dict(live_risk).get("live_risk_report_json") or live_risk_report_path),
                "execution_reconcile_report_json": str(_as_dict(exec_reconcile).get("execution_reconcile_report_json") or execution_reconcile_report_path),
                "kill_switch_report_json": str(_as_dict(kill).get("kill_switch_report_json") or kill_switch_report_path),
                "recovery_report_json": str(_as_dict(recovery).get("recovery_report_json") or recovery_report_path),
            }
            live_feedback = build_live_feedback(
                cycle_id=normalized_cycle_id,
                live_execution_report=live_execution,
            )
            live_feedback_artifacts = persist_live_feedback(
                feedback=live_feedback,
                output_dir=cycle_dir,
            )
            audit_artifacts = persist_operator_audit(
                cycle_id=normalized_cycle_id,
                output_dir=cycle_dir,
                operator_controls=operator_controls,
                capital_governor_report=capital_governor_report,
                session_control_report=session_control_report,
            )
            operator_audit_log_path = Path(audit_artifacts["operator_audit_log_jsonl"])
            control_decision_index_path = Path(audit_artifacts["control_decision_index_json"])
        except Exception as live_exc:
            logger.warning(
                "live_pipeline_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(live_exc)},
            )
            risk_guard_report = {
                "cycle_id": normalized_cycle_id,
                "items": [],
                "summary": {"error": str(live_exc), "candidate_count": 0},
            }
            risk_guard_report_path.write_text(
                json.dumps(risk_guard_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            capital_governor_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "items": [], "summary": {"error": str(live_exc)}}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            session_control_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "items": [], "summary": {"error": str(live_exc)}}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            operator_audit_log_path.write_text("", encoding="utf-8")
            control_decision_index_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "counts": {}, "samples": {}, "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_session_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_profile_report_path.write_text(
                json.dumps(
                    {
                        "cycle_id": normalized_cycle_id,
                        "broker_profile": "paper",
                        "enabled": True,
                        "credential_source": "paper_builtin",
                        "account_mode": "paper",
                        "safe_default": True,
                        "error": str(live_exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            broker_binding_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "binding_mode": "unified_broker_interface", "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            real_broker_dry_run_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "status": "skipped", "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_launch_switch_path.write_text(
                json.dumps({"paper_only": True, "real_broker_enabled": False, "real_broker_dry_run_only": True}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_safety_gate_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "gate_status": "rejected", "executable_real": False, "reasons": ["live_pipeline_failed"]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_readonly_probe_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "status": "controlled_fail", "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_sandbox_orders_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "sandbox_allowed": False, "submitted": [], "canceled": [], "rejected": [{"reasons": ["live_pipeline_failed"]}]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_sandbox_events_path.write_text("", encoding="utf-8")
            broker_reconcile_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "clean_reconcile": False, "summary": {}, "mismatches": [{"type": "missing_order", "reason": "live_pipeline_failed"}]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            eod_closeout_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "closeout_completed": False, "unexplained_hanging": ["live_pipeline_failed"]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_recovery_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "recovery_success_sample": {"status": "controlled_fail"}, "recovery_controlled_fail_sample": {"status": "controlled_fail", "reason": "live_pipeline_failed"}}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_launch_barrier_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "barrier_status": "blocked", "block_reasons": ["live_pipeline_failed"]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            manual_approval_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "status": "rejected", "reasons": ["live_pipeline_failed"]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            canary_launch_gate_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "gate_status": "rejected", "reasons": ["live_pipeline_failed"]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            intraday_monitor_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "status": "critical", "actions": ["halt_canary"], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            post_trade_attribution_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "execution_dragged_result": True, "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            canary_feedback_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "feedback_action": "demote_or_pause", "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_batch_plan_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_status": "blocked", "reason": "live_pipeline_failed", "candidates": []}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_batch_status_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_status": "blocked", "candidates": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            pre_open_prep_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "prep_status": "blocked", "items": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            intraday_batch_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_action": "halt_batch", "items": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            eod_batch_close_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "candidate_outcomes": [], "summary": {"forced_flatten": 0}, "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_ops_summary_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "approved_candidates": [], "launched_candidates": [], "blocked_candidates": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_batch_feedback_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_outcome": "batch_bad", "demoted_subset": [], "paused_candidates": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            trading_day_status_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "current_stage": "archived", "stage_action_summary": {}, "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            ops_control_center_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "system_mode": "paper", "active_batches": [], "active_candidates": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            end_of_day_review_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "candidate_outcomes": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            weekly_candidate_review_path.write_text(
                json.dumps({"week_id": normalized_cycle_id, "keep": [], "promote": ["sample-promote"], "demote": ["sample-demote"], "retire": ["sample-retire"], "watchlist": ["sample-watch"], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            review_plan_patch_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "actions": [{"patch_type": "retire_candidate", "candidate_id": "sample-retire"}, {"patch_type": "tighten_constraints", "candidate_id": "sample-tighten"}], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            human_ops_summary_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "today_status": {"stage": "archived"}, "what_changed_for_tomorrow": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            multi_day_run_plan_path.write_text(
                json.dumps({"start_day": normalized_cycle_id, "end_day": normalized_cycle_id, "target_days": [normalized_cycle_id], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            multi_day_run_report_path.write_text(
                json.dumps({"start_day": normalized_cycle_id, "end_day": normalized_cycle_id, "cycles_run": [], "days_failed": [normalized_cycle_id], "days_recovered": [], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            incident_log_path.write_text("", encoding="utf-8")
            incident_index_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "total_count": 1, "critical_count": 1, "open_count": 1, "resolved_count": 0, "items": [{"incident_id": "sample", "severity": "critical", "summary": "live_pipeline_failed"}]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            recovery_coordinator_report_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "actions": [{"action": "flatten_and_pause", "applied": True}], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            operator_shift_summary_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "open_incidents": 1, "critical_incidents": 1, "next_actions": ["flatten_and_pause"], "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            reliability_metrics_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "cycle_success_rate": 0.0, "broker_probe_success_rate": 0.0, "reconcile_clean_rate": 0.0, "incident_rate": 1.0, "recovery_success_rate": 0.0, "halt_rate": 1.0, "flatten_rate": 1.0, "status": "degraded", "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            ops_registry_path.parent.mkdir(parents=True, exist_ok=True)
            ops_registry_path.write_text(
                json.dumps({"last_cycle_id": normalized_cycle_id, "system_mode": "paper", "active_batches": [], "active_candidates": [], "open_incidents": 1, "current_day_status": "archived", "launch_status": "hold_live", "reliability_status": "degraded", "reason": "live_pipeline_failed"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            orders_state_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "items": [], "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            orders_events_path.write_text("", encoding="utf-8")
            account_snapshot_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            positions_snapshot_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "positions": [], "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            position_reconcile_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "mismatches": [], "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_risk_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "violations": [], "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            execution_reconcile_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "mismatches": [], "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            kill_switch_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "actions": [], "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            recovery_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "status": "failed", "error": str(live_exc)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        reuse_audit_paths = generate_live_reuse_artifacts(
            cycle_id=normalized_cycle_id,
            output_dir=cycle_dir,
        )
        shadow_runs = run_shadow_candidates(
            shadow_candidates_dir=shadow_bundle["shadow_candidates_dir"],
            output_dir=cycle_dir,
        )
        shadow_execution_feedback = build_execution_feedback(
            cycle_id=normalized_cycle_id,
            shadow_runs_dir=shadow_runs["shadow_runs_dir"],
        )
        # Merge shadow and live feedback into one execution feedback stream for downstream policy mapping.
        execution_feedback = {
            "cycle_id": normalized_cycle_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": list(_as_dict(shadow_execution_feedback).get("items") or [])
            + list(_as_dict(live_feedback).get("policy_mapped_feedback") or []),
            "stats": _as_dict(shadow_execution_feedback).get("stats") or {},
        }
        execution_feedback_artifacts = persist_execution_feedback(
            feedback=execution_feedback,
            output_dir=cycle_dir,
        )
        execution_feedback_report_path = cycle_dir / "execution_feedback_report.json"
        execution_feedback_report_payload = {
            "cycle_id": normalized_cycle_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": _as_dict(execution_feedback).get("stats") or {},
            "actions": [
                {
                    "candidate_id": _as_dict(item).get("candidate_id"),
                    "experiment_id": _as_dict(item).get("experiment_id"),
                    "feedback_status": _as_dict(item).get("feedback_status"),
                    "next_action": _as_dict(item).get("next_action"),
                }
                for item in (execution_feedback.get("items") or [])
                if isinstance(item, dict)
            ],
        }
        execution_feedback_report_path.write_text(
            json.dumps(execution_feedback_report_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Lifecycle updates are best-effort and must not block daily runs.
        lifecycle_state_root = Path("runs/ai_trader/research/state")
        candidate_retention_report_path = cycle_dir / "candidate_retention_report.json"
        candidate_gate_report_path = cycle_dir / "candidate_gate_report.json"
        candidate_lifecycle_report_path = cycle_dir / "candidate_lifecycle_report.json"
        admission_gate_path = cycle_dir / "admission_gate.json"
        health_monitor_report_path = cycle_dir / "health_monitor_report.json"
        auto_halt_report_path = cycle_dir / "auto_halt_report.json"
        active_registry_report_path = cycle_dir / "active_registry_report.json"
        preflight_check_path = cycle_dir / "preflight_check.json"
        launch_checklist_path = cycle_dir / "launch_checklist.json"
        readiness_dashboard_path = cycle_dir / "readiness_dashboard.json"
        launch_verdict_path = cycle_dir / "launch_verdict.json"
        evidence_integrity_report_path = cycle_dir / "evidence_integrity_report.json"
        launch_batch_path = cycle_dir / "launch_batch.json"
        ops_summary_path = cycle_dir / "ops_summary.json"
        artifact_index_path = cycle_dir / "artifact_index.json"
        consistency_report_path = cycle_dir / "consistency_report.json"
        postflight_check_path = cycle_dir / "postflight_check.json"
        acceptance_decision_path = cycle_dir / "acceptance_decision.json"
        candidate_lifecycle_index_path = lifecycle_state_root / "candidate_lifecycle_index.json"
        active_registry_path = lifecycle_state_root / "active_registry.json"
        launch_registry_path = lifecycle_state_root / "launch_registry.json"
        system_status_path = lifecycle_state_root / "system_status.json"
        policy_compatibility_summary_path = lifecycle_state_root / "policy_compatibility_summary.json"
        policy_observability_consumers_path = lifecycle_state_root / "policy_observability_consumers.json"
        candidate_lifecycle_index: Dict[str, Any] = {"updated_at": None, "candidates": []}
        candidate_retention_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "items": [], "summary": {}}
        candidate_gate_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "items": [], "summary": {}}
        candidate_lifecycle_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "transitions": [], "summary": {}}
        admission_gate_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "items": [], "summary": {}}
        health_monitor_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "items": [], "summary": {}}
        auto_halt_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "items": [], "summary": {}}
        active_registry_report: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "transitions": [], "summary": {}}
        preflight_check: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "overall_status": "blocked",
            "checks": [],
            "blocking_issues": ["not_generated"],
            "warnings": [],
            "next_actions": ["rerun_preflight_check"],
        }
        launch_checklist: Dict[str, Any] = {"cycle_id": normalized_cycle_id, "steps": [], "summary": {}}
        readiness_dashboard: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "paper_ready": False,
            "live_ready": False,
            "active_candidates": 0,
            "blocked_candidates": 0,
            "critical_alerts": 0,
        }
        launch_verdict: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "verdict": "hold_paper",
            "reason_summary": ["not_generated"],
            "blocking_issues": ["not_generated"],
            "approved_candidates": [],
            "rejected_candidates": [],
        }
        broker_safety_gate: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "broker_profile": "paper",
            "gate_status": "not_applicable",
            "executable_real": False,
            "checks": {},
            "reasons": ["not_generated"],
        }
        evidence_integrity_report: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "overall_status": "blocked",
            "checks": [],
            "missing_evidence": [],
            "blocking_issues": ["not_generated"],
            "next_actions": ["rerun_evidence_integrity"],
        }
        launch_batch: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "batch_id": "",
            "mode": "paper",
            "candidates": [],
            "rejected_candidates": [],
            "total_capital": 0.0,
            "risk_budget": 0.0,
            "status": "blocked",
            "verdict_ref": "launch_verdict.json",
        }
        ops_summary: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "active_candidates": 0,
            "paused_candidates": 0,
            "halted_candidates": 0,
            "today_executed": 0,
            "today_blocked": 0,
            "today_demoted": 0,
            "today_retired": 0,
            "today_live_feedback_issues": 0,
            "next_required_actions": ["collect_runtime_signals"],
        }
        artifact_index: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "run_id": normalized_cycle_id,
            "overall_status": "missing_artifacts",
            "missing_artifacts": ["not_generated"],
            "path_pollution": [],
            "artifacts": [],
        }
        consistency_report: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "run_id": normalized_cycle_id,
            "overall_status": "fail",
            "checks": [],
            "mismatches": ["not_generated"],
        }
        postflight_check: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "run_id": normalized_cycle_id,
            "overall_status": "blocked",
            "blocking_issues": ["not_generated"],
            "warnings": [],
            "next_actions": ["rerun_postflight_check"],
        }
        acceptance_decision: Dict[str, Any] = {
            "cycle_id": normalized_cycle_id,
            "run_id": normalized_cycle_id,
            "decision": "hold",
            "accepted": False,
            "reasons": ["not_generated"],
            "required_actions": ["rerun_acceptance_decision"],
        }
        try:
            loaded_lifecycle = load_candidate_lifecycle_index(state_root=lifecycle_state_root)
            candidate_retention_report = build_candidate_retention(
                cycle_id=normalized_cycle_id,
                execution_feedback=execution_feedback,
                lifecycle_index=loaded_lifecycle,
            )
            candidate_gate_report = build_candidate_gate(
                cycle_id=normalized_cycle_id,
                lifecycle_index=loaded_lifecycle,
                promotion_gate=promotion_gate,
                execution_feedback=execution_feedback,
                retention_report=candidate_retention_report,
                min_cycles_for_promote=1,
            )
            lifecycle_apply = apply_candidate_lifecycle(
                cycle_id=normalized_cycle_id,
                lifecycle_index=loaded_lifecycle,
                candidate_gate_report=candidate_gate_report,
                candidate_retention_report=candidate_retention_report,
            )
            candidate_lifecycle_index = _as_dict(lifecycle_apply.get("index"))
            candidate_lifecycle_report = _as_dict(lifecycle_apply.get("report"))
            candidate_lifecycle_index_path = Path(
                persist_candidate_lifecycle_index(index=candidate_lifecycle_index, state_root=lifecycle_state_root)
            )
        except Exception as lifecycle_exc:
            logger.warning(
                "candidate_lifecycle_update_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(lifecycle_exc)},
            )
            candidate_lifecycle_report = {
                "cycle_id": normalized_cycle_id,
                "transitions": [],
                "summary": {"error": str(lifecycle_exc)},
            }
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
        try:
            admission_gate_report = build_admission_gate(
                cycle_id=normalized_cycle_id,
                candidate_lifecycle_index=candidate_lifecycle_index,
                candidate_retention_report=candidate_retention_report,
                risk_guard_report=risk_guard_report,
                min_cycles_observed=1,
                min_shadow_runs=1,
                max_avg_slippage_bps=10.0,
                min_fill_rate=0.75,
            )
            health_monitor_report = build_health_monitor_report(
                cycle_id=normalized_cycle_id,
                live_execution_report=live_execution,
                live_feedback=live_feedback,
                risk_guard_report=risk_guard_report,
            )
            auto_halt_report = build_auto_halt_report(
                cycle_id=normalized_cycle_id,
                health_monitor_report=health_monitor_report,
                admission_gate=admission_gate_report,
            )
            existing_registry = load_active_registry(state_root=lifecycle_state_root)
            registry_update = update_active_registry(
                cycle_id=normalized_cycle_id,
                existing_registry=existing_registry,
                admission_gate=admission_gate_report,
                health_monitor_report=health_monitor_report,
                auto_halt_report=auto_halt_report,
            )
            active_registry_path = Path(
                persist_active_registry(
                    registry=_as_dict(registry_update.get("registry")),
                    state_root=lifecycle_state_root,
                )
            )
            active_registry_report = _as_dict(registry_update.get("report"))
        except Exception as admission_exc:
            logger.warning(
                "admission_health_halt_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(admission_exc)},
            )
            active_registry_report = {
                "cycle_id": normalized_cycle_id,
                "summary": {"error": str(admission_exc)},
                "transitions": [],
            }
        admission_gate_path.write_text(
            json.dumps(admission_gate_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        health_monitor_report_path.write_text(
            json.dumps(health_monitor_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        auto_halt_report_path.write_text(
            json.dumps(auto_halt_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        active_registry_report_path.write_text(
            json.dumps(active_registry_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            active_registry_payload = _load_json_safe(active_registry_path)
            live_feedback_index_payload = _load_json_safe(Path(live_feedback_artifacts["live_feedback_index_json"]))
            required_artifacts = [
                str(operator_controls_path),
                str(capital_governor_report_path),
                str(session_control_report_path),
                str(admission_gate_path),
                str(health_monitor_report_path),
                str(active_registry_path),
                str(candidate_lifecycle_index_path),
                str(Path(live_feedback_artifacts["live_feedback_index_json"])),
                str(evaluation["artifacts"]["leaderboard_json"]),
                str(promotion_gate_path),
            ]
            evidence_integrity_report = build_evidence_integrity_report(
                cycle_id=normalized_cycle_id,
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
            policy_context = _load_preflight_policy_context(
                state_root=state_root,
                cycle_dir=cycle_dir,
            )
            preflight_check = build_preflight_check(
                cycle_id=normalized_cycle_id,
                operator_controls=operator_controls,
                capital_governor_report=_load_json_safe(capital_governor_report_path),
                session_control_report=_load_json_safe(session_control_report_path),
                admission_gate=admission_gate_report,
                health_monitor_report=health_monitor_report,
                active_registry=active_registry_payload,
                candidate_lifecycle_index=candidate_lifecycle_index,
                live_feedback_index=live_feedback_index_payload,
                required_artifacts=required_artifacts,
                evidence_integrity_report=evidence_integrity_report,
                preflight_policy=_as_dict(policy_context.get("preflight_policy")),
                preflight_history=policy_context.get("preflight_history"),
            )
            policy_compatibility_summary_bundle = persist_policy_compatibility_summary(
                cycle_id=normalized_cycle_id,
                preflight_check=preflight_check,
                state_root=lifecycle_state_root,
                cycles_root=RESEARCH_CYCLES_ROOT,
            )
            policy_compatibility_summary_path = Path(
                str(policy_compatibility_summary_bundle.get("policy_compatibility_summary_json") or policy_compatibility_summary_path)
            )
            policy_observability_consumers_bundle = persist_policy_observability_consumer_inventory(
                state_root=lifecycle_state_root,
                repo_root=Path("."),
            )
            policy_observability_consumers_path = Path(
                str(policy_observability_consumers_bundle.get("policy_observability_consumers_json") or policy_observability_consumers_path)
            )
            readiness_dashboard = build_readiness_dashboard(
                cycle_id=normalized_cycle_id,
                preflight_check=preflight_check,
                admission_gate=admission_gate_report,
                health_monitor_report=health_monitor_report,
                active_registry=active_registry_payload,
                policy_compatibility_summary_path=policy_compatibility_summary_path,
                policy_observability_consumers_path=policy_observability_consumers_path,
            )
            launch_checklist = build_launch_checklist(
                cycle_id=normalized_cycle_id,
                preflight_check=preflight_check,
                readiness_dashboard=readiness_dashboard,
            )
            launch_verdict = build_launch_verdict(
                cycle_id=normalized_cycle_id,
                preflight_check=preflight_check,
                launch_checklist=launch_checklist,
                readiness_dashboard=readiness_dashboard,
                admission_gate=admission_gate_report,
                policy_compatibility_summary_path=policy_compatibility_summary_path,
                policy_observability_consumers_path=policy_observability_consumers_path,
            )
            broker_safety_gate_bundle = build_broker_safety_gate(
                cycle_id=normalized_cycle_id,
                broker_profile_report=_load_json_safe(Path(broker_pipeline["broker_profile_report_json"])),
                operator_controls=operator_controls,
                preflight_check=preflight_check,
                admission_gate=admission_gate_report,
                health_monitor_report=health_monitor_report,
                kill_switch_report=_load_json_safe(kill_switch_report_path),
                output_dir=cycle_dir,
            )
            broker_safety_gate = _as_dict(broker_safety_gate_bundle.get("broker_safety_gate"))
            launch_batch = build_launch_batch(
                cycle_id=normalized_cycle_id,
                mode="small_live" if str(launch_verdict.get("verdict")) == "go_small_live" else "paper",
                launch_verdict=launch_verdict,
                admission_gate=admission_gate_report,
                allocation_plan=_load_json_safe(allocation_plan_path),
                portfolio_selection=_load_json_safe(portfolio_selection_path),
                evidence_integrity_report=evidence_integrity_report,
                min_candidates=2,
            )
            ops_summary = build_ops_summary(
                cycle_id=normalized_cycle_id,
                active_registry=active_registry_payload,
                auto_halt_report=auto_halt_report,
                candidate_lifecycle_report=candidate_lifecycle_report,
                live_feedback=live_feedback,
            )
        except Exception as readiness_exc:
            logger.warning(
                "readiness_bundle_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(readiness_exc)},
            )
            preflight_check = {
                "cycle_id": normalized_cycle_id,
                "overall_status": "blocked",
                "checks": [],
                "blocking_issues": [f"readiness_bundle_failed:{readiness_exc}"],
                "warnings": [],
                "next_actions": ["check_readiness_bundle_logs"],
            }
            readiness_dashboard = {
                "cycle_id": normalized_cycle_id,
                "paper_ready": False,
                "live_ready": False,
                "active_candidates": 0,
                "blocked_candidates": 0,
                "critical_alerts": 0,
            }
            launch_checklist = {
                "cycle_id": normalized_cycle_id,
                "steps": [],
                "summary": {"error": str(readiness_exc)},
            }
            launch_verdict = {
                "cycle_id": normalized_cycle_id,
                "verdict": "hold_paper",
                "reason_summary": ["readiness_bundle_failed"],
                "blocking_issues": [str(readiness_exc)],
                "approved_candidates": [],
                "rejected_candidates": [],
            }
            broker_safety_gate = {
                "cycle_id": normalized_cycle_id,
                "broker_profile": "paper",
                "gate_status": "rejected",
                "executable_real": False,
                "checks": {},
                "reasons": [f"readiness_bundle_failed:{readiness_exc}"],
            }
            evidence_integrity_report = {
                "cycle_id": normalized_cycle_id,
                "overall_status": "blocked",
                "checks": [],
                "missing_evidence": [],
                "blocking_issues": [f"readiness_bundle_failed:{readiness_exc}"],
                "next_actions": ["check_readiness_bundle_logs"],
            }
        active_registry_payload_final = _load_json_safe(active_registry_path)
        launch_registry_path = Path(
            persist_launch_registry(
                cycle_id=normalized_cycle_id,
                launch_verdict=launch_verdict,
                launch_batch=launch_batch,
                active_registry=active_registry_payload_final,
                state_root=lifecycle_state_root,
            )
        )
        system_status_path = Path(
            persist_system_status(
                cycle_id=normalized_cycle_id,
                operator_controls=operator_controls,
                risk_guard_report=risk_guard_report,
                launch_verdict=launch_verdict,
                state_root=lifecycle_state_root,
            )
        )
        broker_profile_payload = _load_json_safe(Path(broker_pipeline["broker_profile_report_json"]))
        broker_readonly_probe_bundle = run_broker_readonly_probe(
            cycle_id=normalized_cycle_id,
            broker=broker_adapter,
            broker_profile=str(broker_profile_payload.get("broker_profile") or "paper"),
            output_dir=cycle_dir,
        )
        sandbox_bundle = run_broker_sandbox_orders(
            cycle_id=normalized_cycle_id,
            broker_profile_report=broker_profile_payload,
            broker_safety_gate=broker_safety_gate,
            operator_controls=operator_controls,
            risk_guard_report=risk_guard_report,
            capital_governor_report=_load_json_safe(capital_governor_report_path),
            session_control_report=_load_json_safe(session_control_report_path),
            output_dir=cycle_dir,
        )
        broker_reconcile_bundle = run_broker_reconcile(
            cycle_id=normalized_cycle_id,
            orders_state=_load_json_safe(orders_state_path),
            broker_snapshot=_load_json_safe(cycle_dir / "broker_session_state.json"),
            positions_snapshot=_load_json_safe(positions_snapshot_path),
            output_dir=cycle_dir,
        )
        eod_bundle = run_eod_closeout(
            cycle_id=normalized_cycle_id,
            broker_snapshot=_load_json_safe(cycle_dir / "broker_session_state.json"),
            kill_switch_report=_load_json_safe(kill_switch_report_path),
            output_dir=cycle_dir,
        )
        broker_recovery_bundle = run_broker_recovery(
            cycle_id=normalized_cycle_id,
            broker=broker_adapter,
            broker_session=_load_json_safe(broker_session_path),
            output_dir=cycle_dir,
        )
        barrier_bundle = apply_broker_launch_barrier(
            cycle_id=normalized_cycle_id,
            broker_readonly_probe=_as_dict(broker_readonly_probe_bundle.get("broker_readonly_probe")),
            broker_reconcile_report=_as_dict(broker_reconcile_bundle.get("broker_reconcile_report")),
            eod_closeout_report=_as_dict(eod_bundle.get("eod_closeout_report")),
            launch_registry=_load_json_safe(launch_registry_path),
            active_registry=_load_json_safe(active_registry_path),
            system_status=_load_json_safe(system_status_path),
            state_root=lifecycle_state_root,
            output_dir=cycle_dir,
        )
        broker_pipeline["broker_readonly_probe_json"] = str(broker_readonly_probe_bundle["broker_readonly_probe_json"])
        broker_pipeline["broker_sandbox_orders_json"] = str(sandbox_bundle["broker_sandbox_orders_json"])
        broker_pipeline["broker_sandbox_events_jsonl"] = str(sandbox_bundle["broker_sandbox_events_jsonl"])
        broker_pipeline["broker_reconcile_report_json"] = str(broker_reconcile_bundle["broker_reconcile_report_json"])
        broker_pipeline["eod_closeout_report_json"] = str(eod_bundle["eod_closeout_report_json"])
        broker_pipeline["broker_recovery_report_json"] = str(broker_recovery_bundle["broker_recovery_report_json"])
        broker_pipeline["broker_launch_barrier_json"] = str(barrier_bundle["broker_launch_barrier_json"])
        # barrier may update these state files
        launch_registry_path = Path(lifecycle_state_root) / "launch_registry.json"
        active_registry_path = Path(lifecycle_state_root) / "active_registry.json"
        system_status_path = Path(lifecycle_state_root) / "system_status.json"
        active_registry_after_barrier = _load_json_safe(active_registry_path)
        admission_items = [_as_dict(x) for x in _as_list(admission_gate_report.get("items"))]
        candidate_id = ""
        symbol = "SPY"
        for row in _as_list(active_registry_after_barrier.get("items")):
            item = _as_dict(row)
            if str(item.get("status") or "") == "active":
                candidate_id = str(item.get("candidate_id") or "")
                break
        if not candidate_id and admission_items:
            candidate_id = str(admission_items[0].get("candidate_id") or "")
        if not candidate_id:
            candidate_id = "canary-default-candidate"
        for row in admission_items:
            if str(row.get("candidate_id") or "") == candidate_id:
                sym_guess = str(row.get("symbol") or "")
                if sym_guess:
                    symbol = sym_guess.upper()
                break
        manual_approval_bundle = build_manual_approval_report(
            cycle_id=normalized_cycle_id,
            candidate_id=candidate_id,
            output_dir=cycle_dir,
            state_root=lifecycle_state_root,
        )
        canary_gate_bundle = build_canary_launch_gate(
            cycle_id=normalized_cycle_id,
            candidate_id=candidate_id,
            launch_verdict=launch_verdict,
            broker_safety_gate=broker_safety_gate,
            broker_launch_barrier=_as_dict(barrier_bundle.get("broker_launch_barrier")),
            operator_controls=operator_controls,
            broker_launch_switch=_load_json_safe(Path(broker_pipeline["broker_launch_switch_json"])),
            manual_approval_report=_as_dict(manual_approval_bundle.get("manual_approval_report")),
            candidate_lifecycle_index=load_candidate_lifecycle_index(state_root=lifecycle_state_root),
            output_dir=cycle_dir,
        )
        canary_exec_bundle = run_canary_executor(
            cycle_id=normalized_cycle_id,
            candidate_id=candidate_id,
            symbol=symbol,
            canary_launch_gate=_as_dict(canary_gate_bundle.get("canary_launch_gate")),
            manual_approval_report=_as_dict(manual_approval_bundle.get("manual_approval_report")),
            output_dir=cycle_dir,
        )
        intraday_bundle = build_intraday_monitor_report(
            cycle_id=normalized_cycle_id,
            canary_run_summary=_as_dict(canary_exec_bundle.get("canary_run_summary")),
            output_dir=cycle_dir,
        )
        attribution_bundle = build_post_trade_attribution(
            cycle_id=normalized_cycle_id,
            canary_run_summary=_as_dict(canary_exec_bundle.get("canary_run_summary")),
            intraday_monitor_report=_as_dict(intraday_bundle.get("intraday_monitor_report")),
            output_dir=cycle_dir,
        )
        canary_feedback_bundle = apply_canary_feedback(
            cycle_id=normalized_cycle_id,
            candidate_id=candidate_id,
            post_trade_attribution=_as_dict(attribution_bundle.get("post_trade_attribution")),
            intraday_monitor_report=_as_dict(intraday_bundle.get("intraday_monitor_report")),
            candidate_lifecycle_index=load_candidate_lifecycle_index(state_root=lifecycle_state_root),
            active_registry=_load_json_safe(active_registry_path),
            launch_registry=_load_json_safe(launch_registry_path),
            system_status=_load_json_safe(system_status_path),
            state_root=lifecycle_state_root,
            output_dir=cycle_dir,
        )
        execution_feedback_item = _as_dict(canary_feedback_bundle.get("execution_feedback_item"))
        if execution_feedback_item:
            execution_feedback.setdefault("items", []).append(execution_feedback_item)
            execution_feedback_artifacts["execution_feedback_json"] = persist_execution_feedback(
                feedback=execution_feedback,
                output_dir=cycle_dir,
            )["execution_feedback_json"]
        candidate_lifecycle_index = load_candidate_lifecycle_index(state_root=lifecycle_state_root)
        broker_pipeline["canary_launch_gate_json"] = str(canary_gate_bundle["canary_launch_gate_json"])
        broker_pipeline["manual_approval_report_json"] = str(manual_approval_bundle["manual_approval_report_json"])
        broker_pipeline["canary_run_summary_json"] = str(canary_exec_bundle["canary_run_summary_json"])
        broker_pipeline["intraday_monitor_report_json"] = str(intraday_bundle["intraday_monitor_report_json"])
        broker_pipeline["post_trade_attribution_json"] = str(attribution_bundle["post_trade_attribution_json"])
        broker_pipeline["canary_feedback_report_json"] = str(canary_feedback_bundle["canary_feedback_report_json"])
        try:
            batch_bundle = build_live_batch_plan(
                cycle_id=normalized_cycle_id,
                launch_batch=launch_batch,
                admission_gate=admission_gate_report,
                capital_governor_report=_load_json_safe(capital_governor_report_path),
                risk_guard_report=_load_json_safe(risk_guard_report_path),
                mode="small_live" if str(launch_verdict.get("verdict") or "") == "go_small_live" else "paper",
                output_dir=cycle_dir,
            )
            pre_open_bundle = run_pre_open_prep(
                cycle_id=normalized_cycle_id,
                live_batch_plan=_as_dict(batch_bundle.get("live_batch_plan")),
                manual_approvals=_load_json_safe(Path(state_root) / "manual_approval.json"),
                broker_readonly_probe=_as_dict(broker_readonly_probe_bundle.get("broker_readonly_probe")),
                account_snapshot=_load_json_safe(account_snapshot_path),
                positions_snapshot=_load_json_safe(positions_snapshot_path),
                operator_controls=operator_controls,
                session_control_report=_load_json_safe(session_control_report_path),
                kill_switch_report=_load_json_safe(kill_switch_report_path),
                output_dir=cycle_dir,
            )
            intraday_batch_bundle = run_intraday_batch_supervisor(
                cycle_id=normalized_cycle_id,
                live_batch_plan=_as_dict(batch_bundle.get("live_batch_plan")),
                pre_open_prep_report=_as_dict(pre_open_bundle.get("pre_open_prep_report")),
                risk_guard_report=_load_json_safe(risk_guard_report_path),
                output_dir=cycle_dir,
            )
            eod_batch_bundle = run_eod_batch_close(
                cycle_id=normalized_cycle_id,
                live_batch_plan=_as_dict(batch_bundle.get("live_batch_plan")),
                intraday_batch_report=_as_dict(intraday_batch_bundle.get("intraday_batch_report")),
                broker_reconcile_report=_as_dict(broker_reconcile_bundle.get("broker_reconcile_report")),
                output_dir=cycle_dir,
            )
            live_ops_bundle = build_live_ops_summary(
                cycle_id=normalized_cycle_id,
                live_batch_plan=_as_dict(batch_bundle.get("live_batch_plan")),
                pre_open_prep_report=_as_dict(pre_open_bundle.get("pre_open_prep_report")),
                intraday_batch_report=_as_dict(intraday_batch_bundle.get("intraday_batch_report")),
                eod_batch_close_report=_as_dict(eod_batch_bundle.get("eod_batch_close_report")),
                output_dir=cycle_dir,
            )
            live_batch_feedback_bundle = apply_live_batch_feedback(
                cycle_id=normalized_cycle_id,
                live_batch_plan=_as_dict(batch_bundle.get("live_batch_plan")),
                intraday_batch_report=_as_dict(intraday_batch_bundle.get("intraday_batch_report")),
                eod_batch_close_report=_as_dict(eod_batch_bundle.get("eod_batch_close_report")),
                candidate_lifecycle_index=load_candidate_lifecycle_index(state_root=lifecycle_state_root),
                active_registry=_load_json_safe(active_registry_path),
                launch_registry=_load_json_safe(launch_registry_path),
                state_root=lifecycle_state_root,
                output_dir=cycle_dir,
            )
            feedback_rows = _as_list(_as_dict(live_batch_feedback_bundle.get("live_batch_feedback_report")).get("feedback_items"))
            if feedback_rows:
                execution_feedback.setdefault("items", []).extend(feedback_rows)
                execution_feedback_artifacts["execution_feedback_json"] = persist_execution_feedback(
                    feedback=execution_feedback,
                    output_dir=cycle_dir,
                )["execution_feedback_json"]
            broker_pipeline["live_batch_plan_json"] = str(batch_bundle["live_batch_plan_json"])
            broker_pipeline["live_batch_status_json"] = str(batch_bundle["live_batch_status_json"])
            broker_pipeline["pre_open_prep_report_json"] = str(pre_open_bundle["pre_open_prep_report_json"])
            broker_pipeline["intraday_batch_report_json"] = str(intraday_batch_bundle["intraday_batch_report_json"])
            broker_pipeline["eod_batch_close_report_json"] = str(eod_batch_bundle["eod_batch_close_report_json"])
            broker_pipeline["live_ops_summary_json"] = str(live_ops_bundle["live_ops_summary_json"])
            broker_pipeline["live_batch_feedback_report_json"] = str(live_batch_feedback_bundle["live_batch_feedback_report_json"])
        except Exception as batch_exc:
            logger.warning(
                "live_batch_stage_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(batch_exc)},
            )
            live_batch_plan_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_status": "blocked", "reason": f"live_batch_stage_failed:{batch_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_batch_status_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_status": "blocked", "reason": f"live_batch_stage_failed:{batch_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            pre_open_prep_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "prep_status": "blocked", "reason": f"live_batch_stage_failed:{batch_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            intraday_batch_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_action": "halt_batch", "reason": f"live_batch_stage_failed:{batch_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            eod_batch_close_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "summary": {"forced_flatten": 0}, "reason": f"live_batch_stage_failed:{batch_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_ops_summary_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "approved_candidates": [], "reason": f"live_batch_stage_failed:{batch_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            live_batch_feedback_report_path.write_text(
                json.dumps({"cycle_id": normalized_cycle_id, "batch_outcome": "batch_bad", "reason": f"live_batch_stage_failed:{batch_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            broker_pipeline["live_batch_plan_json"] = str(live_batch_plan_path)
            broker_pipeline["live_batch_status_json"] = str(live_batch_status_path)
            broker_pipeline["pre_open_prep_report_json"] = str(pre_open_prep_report_path)
            broker_pipeline["intraday_batch_report_json"] = str(intraday_batch_report_path)
            broker_pipeline["eod_batch_close_report_json"] = str(eod_batch_close_report_path)
            broker_pipeline["live_ops_summary_json"] = str(live_ops_summary_path)
            broker_pipeline["live_batch_feedback_report_json"] = str(live_batch_feedback_report_path)
        try:
            trading_day_bundle = build_trading_day_status(
                day_id=normalized_cycle_id,
                pre_open_prep_report=_load_json_safe(pre_open_prep_report_path),
                intraday_batch_report=_load_json_safe(intraday_batch_report_path),
                eod_batch_close_report=_load_json_safe(eod_batch_close_report_path),
                output_dir=cycle_dir,
            )
            ops_center_bundle = build_ops_control_center(
                day_id=normalized_cycle_id,
                live_batch_plan=_load_json_safe(live_batch_plan_path),
                live_batch_status=_load_json_safe(live_batch_status_path),
                intraday_batch_report=_load_json_safe(intraday_batch_report_path),
                live_ops_summary=_load_json_safe(live_ops_summary_path),
                capital_governor_report=_load_json_safe(capital_governor_report_path),
                operator_controls=operator_controls,
                output_dir=cycle_dir,
            )
            eod_review_bundle = build_end_of_day_review(
                day_id=normalized_cycle_id,
                live_batch_plan=_load_json_safe(live_batch_plan_path),
                intraday_batch_report=_load_json_safe(intraday_batch_report_path),
                eod_batch_close_report=_load_json_safe(eod_batch_close_report_path),
                live_ops_summary=_load_json_safe(live_ops_summary_path),
                output_dir=cycle_dir,
            )
            weekly_review_bundle = build_weekly_candidate_review(
                week_id=normalized_cycle_id,
                candidate_lifecycle_index=load_candidate_lifecycle_index(state_root=lifecycle_state_root),
                candidate_retention_report=_load_json_safe(candidate_retention_report_path),
                live_feedback_index=_load_json_safe(live_feedback_artifacts.get("live_feedback_index_json", "")),
                output_dir=cycle_dir,
            )
            review_patch_bundle = build_review_plan_patch(
                day_id=normalized_cycle_id,
                end_of_day_review=_as_dict(eod_review_bundle.get("end_of_day_review")),
                weekly_candidate_review=_as_dict(weekly_review_bundle.get("weekly_candidate_review")),
                output_dir=cycle_dir,
            )
            human_ops_bundle = build_human_ops_summary(
                day_id=normalized_cycle_id,
                trading_day_status=_as_dict(trading_day_bundle.get("trading_day_status")),
                ops_control_center=_as_dict(ops_center_bundle.get("ops_control_center")),
                end_of_day_review=_as_dict(eod_review_bundle.get("end_of_day_review")),
                weekly_candidate_review=_as_dict(weekly_review_bundle.get("weekly_candidate_review")),
                review_plan_patch=_as_dict(review_patch_bundle.get("review_plan_patch")),
                output_dir=cycle_dir,
                policy_compatibility_summary_path=policy_compatibility_summary_path,
                policy_observability_consumers_path=policy_observability_consumers_path,
            )
            trading_day_status_path = Path(str(trading_day_bundle["trading_day_status_json"]))
            ops_control_center_path = Path(str(ops_center_bundle["ops_control_center_json"]))
            end_of_day_review_path = Path(str(eod_review_bundle["end_of_day_review_json"]))
            weekly_candidate_review_path = Path(str(weekly_review_bundle["weekly_candidate_review_json"]))
            review_plan_patch_path = Path(str(review_patch_bundle["review_plan_patch_json"]))
            human_ops_summary_path = Path(str(human_ops_bundle["human_ops_summary_json"]))
        except Exception as ops_center_exc:
            logger.warning(
                "ops_control_center_stage_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(ops_center_exc)},
            )
            trading_day_status_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "current_stage": "archived", "reason": f"ops_control_center_stage_failed:{ops_center_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            ops_control_center_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "active_batches": [], "reason": f"ops_control_center_stage_failed:{ops_center_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            end_of_day_review_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "candidate_outcomes": [], "reason": f"ops_control_center_stage_failed:{ops_center_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            weekly_candidate_review_path.write_text(
                json.dumps({"week_id": normalized_cycle_id, "keep": [], "promote": ["sample-promote"], "demote": ["sample-demote"], "retire": ["sample-retire"], "watchlist": ["sample-watch"], "reason": f"ops_control_center_stage_failed:{ops_center_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            review_plan_patch_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "actions": [{"patch_type": "retire_candidate", "candidate_id": "sample-retire"}, {"patch_type": "tighten_constraints", "candidate_id": "sample-tighten"}], "reason": f"ops_control_center_stage_failed:{ops_center_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            human_ops_summary_path.write_text(
                json.dumps({"day_id": normalized_cycle_id, "today_status": {"stage": "archived"}, "what_changed_for_tomorrow": [], "reason": f"ops_control_center_stage_failed:{ops_center_exc}"}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        try:
            live_batch_plan_payload = _load_json_safe(live_batch_plan_path)
            incident_bundle = build_incident_artifacts(
                day_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                batch_id=str(_as_dict(live_batch_plan_payload).get("batch_id") or ""),
                intraday_batch_report=_load_json_safe(intraday_batch_report_path),
                broker_readonly_probe=_load_json_safe(broker_readonly_probe_path),
                broker_reconcile_report=_load_json_safe(broker_reconcile_report_path),
                kill_switch_report=_load_json_safe(kill_switch_report_path),
                output_dir=cycle_dir,
                state_root=lifecycle_state_root,
            )
            recovery_coord_bundle = build_recovery_coordinator_report(
                day_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                incident_index=_as_dict(incident_bundle.get("incident_index")),
                trading_day_status=_load_json_safe(trading_day_status_path),
                output_dir=cycle_dir,
            )
            multi_day_bundle = build_multi_day_run_artifacts(
                day_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                trading_day_status=_load_json_safe(trading_day_status_path),
                incident_index=_as_dict(incident_bundle.get("incident_index")),
                recovery_coordinator_report=_as_dict(recovery_coord_bundle.get("recovery_coordinator_report")),
                output_dir=cycle_dir,
            )
            reliability_bundle = build_reliability_metrics(
                day_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                multi_day_run_report=_as_dict(multi_day_bundle.get("multi_day_run_report")),
                incident_index=_as_dict(incident_bundle.get("incident_index")),
                broker_readonly_probe=_load_json_safe(broker_readonly_probe_path),
                broker_reconcile_report=_load_json_safe(broker_reconcile_report_path),
                recovery_coordinator_report=_as_dict(recovery_coord_bundle.get("recovery_coordinator_report")),
                live_ops_summary=_load_json_safe(live_ops_summary_path),
                output_dir=cycle_dir,
            )
            shift_bundle = build_operator_shift_summary(
                day_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                incident_index=_as_dict(incident_bundle.get("incident_index")),
                active_registry=_load_json_safe(active_registry_path),
                ops_control_center=_load_json_safe(ops_control_center_path),
                recovery_coordinator_report=_as_dict(recovery_coord_bundle.get("recovery_coordinator_report")),
                output_dir=cycle_dir,
            )
            ops_registry_bundle = update_ops_registry(
                day_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                system_mode=str(_as_dict(live_batch_plan_payload).get("mode") or "paper"),
                trading_day_status=_load_json_safe(trading_day_status_path),
                launch_status=launch_verdict,
                ops_control_center=_load_json_safe(ops_control_center_path),
                incident_index=_as_dict(incident_bundle.get("incident_index")),
                reliability_metrics=_as_dict(reliability_bundle.get("reliability_metrics")),
                state_root=lifecycle_state_root,
            )
            multi_day_run_plan_path = Path(str(multi_day_bundle["multi_day_run_plan_json"]))
            multi_day_run_report_path = Path(str(multi_day_bundle["multi_day_run_report_json"]))
            incident_log_path = Path(str(incident_bundle["incident_log_jsonl"]))
            incident_index_path = Path(str(incident_bundle["incident_index_json"]))
            recovery_coordinator_report_path = Path(str(recovery_coord_bundle["recovery_coordinator_report_json"]))
            operator_shift_summary_path = Path(str(shift_bundle["operator_shift_summary_json"]))
            reliability_metrics_path = Path(str(reliability_bundle["reliability_metrics_json"]))
            ops_registry_path = Path(str(ops_registry_bundle["ops_registry_json"]))
        except Exception as hardening_exc:
            logger.warning(
                "production_ops_hardening_stage_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(hardening_exc)},
            )
            write_json_crash_safe(
                multi_day_run_plan_path,
                {"start_day": normalized_cycle_id, "end_day": normalized_cycle_id, "target_days": [normalized_cycle_id], "reason": f"production_ops_hardening_stage_failed:{hardening_exc}"},
            )
            write_json_crash_safe(
                multi_day_run_report_path,
                {"start_day": normalized_cycle_id, "end_day": normalized_cycle_id, "run_id": normalized_cycle_id, "cycles_run": [normalized_cycle_id], "days_failed": [normalized_cycle_id], "days_recovered": [], "reason": f"production_ops_hardening_stage_failed:{hardening_exc}"},
            )
            incident_log_path.write_text("", encoding="utf-8")
            write_json_crash_safe(
                incident_index_path,
                {"day_id": normalized_cycle_id, "run_id": normalized_cycle_id, "total_count": 1, "critical_count": 1, "open_count": 1, "resolved_count": 0, "items": [{"incident_id": "sample", "severity": "critical", "summary": "hardening_stage_failed"}], "reason": f"production_ops_hardening_stage_failed:{hardening_exc}"},
            )
            write_json_crash_safe(
                recovery_coordinator_report_path,
                {"day_id": normalized_cycle_id, "run_id": normalized_cycle_id, "actions": [{"action": "flatten_and_pause", "applied": True}, {"action": "safe_resume_next_day", "applied": True}], "reason": f"production_ops_hardening_stage_failed:{hardening_exc}"},
            )
            write_json_crash_safe(
                operator_shift_summary_path,
                {"day_id": normalized_cycle_id, "run_id": normalized_cycle_id, "open_incidents": 1, "critical_incidents": 1, "next_actions": ["flatten_and_pause"], "reason": f"production_ops_hardening_stage_failed:{hardening_exc}"},
            )
            write_json_crash_safe(
                reliability_metrics_path,
                {"day_id": normalized_cycle_id, "run_id": normalized_cycle_id, "cycle_success_rate": 0.0, "broker_probe_success_rate": 0.0, "reconcile_clean_rate": 0.0, "incident_rate": 1.0, "recovery_success_rate": 0.0, "halt_rate": 1.0, "flatten_rate": 1.0, "status": "degraded", "reason": f"production_ops_hardening_stage_failed:{hardening_exc}"},
            )
            ops_registry_path.parent.mkdir(parents=True, exist_ok=True)
            write_json_crash_safe(
                ops_registry_path,
                {"last_cycle_id": normalized_cycle_id, "run_id": normalized_cycle_id, "system_mode": "paper", "active_batches": [], "active_candidates": [], "open_incidents": 1, "current_day_status": "archived", "launch_status": "hold_live", "reliability_status": "degraded", "reason": f"production_ops_hardening_stage_failed:{hardening_exc}"},
            )
        if not ops_summary or str(_as_dict(ops_summary).get("cycle_id") or "") != normalized_cycle_id:
            ops_summary = build_ops_summary(
                cycle_id=normalized_cycle_id,
                active_registry=_load_json_safe(active_registry_path),
                auto_halt_report=auto_halt_report,
                candidate_lifecycle_report=candidate_lifecycle_report,
                live_feedback=live_feedback,
            )
        # Persist preflight/verdict/evidence first so artifact index sees final existence state.
        write_json_crash_safe(preflight_check_path, preflight_check)
        write_json_crash_safe(launch_verdict_path, launch_verdict)
        write_json_crash_safe(evidence_integrity_report_path, evidence_integrity_report)
        try:
            required_artifacts_for_index = {
                "preflight_check": preflight_check_path,
                "launch_verdict": launch_verdict_path,
                "evidence_integrity_report": evidence_integrity_report_path,
                "incident_index": incident_index_path,
                "recovery_coordinator_report": recovery_coordinator_report_path,
                "operator_shift_summary": operator_shift_summary_path,
                "reliability_metrics": reliability_metrics_path,
                "ops_registry": ops_registry_path,
                "task_manifest": task_manifest_path,
                "task_outcome_index": task_outcome_index_path,
            }
            artifact_index_bundle = build_artifact_index(
                cycle_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                cycle_dir=cycle_dir,
                state_root=lifecycle_state_root,
                required_artifacts=required_artifacts_for_index,
            )
            artifact_index = _as_dict(artifact_index_bundle.get("artifact_index"))
            artifact_index_path = Path(str(artifact_index_bundle.get("artifact_index_json") or artifact_index_path))
            consistency_bundle = build_consistency_report(
                cycle_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                incident_index=_load_json_safe(incident_index_path),
                recovery_report=_load_json_safe(recovery_coordinator_report_path),
                shift_summary=_load_json_safe(operator_shift_summary_path),
                reliability_metrics=_load_json_safe(reliability_metrics_path),
                ops_registry=_load_json_safe(ops_registry_path),
                artifact_index=artifact_index,
                output_dir=cycle_dir,
            )
            consistency_report = _as_dict(consistency_bundle.get("consistency_report"))
            consistency_report_path = Path(str(consistency_bundle.get("consistency_report_json") or consistency_report_path))
            postflight_bundle = build_postflight_check(
                cycle_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                preflight_check=preflight_check,
                consistency_report=consistency_report,
                recovery_report=_load_json_safe(recovery_coordinator_report_path),
                incident_index=_load_json_safe(incident_index_path),
                reliability_metrics=_load_json_safe(reliability_metrics_path),
                output_dir=cycle_dir,
            )
            postflight_check = _as_dict(postflight_bundle.get("postflight_check"))
            postflight_check_path = Path(str(postflight_bundle.get("postflight_check_json") or postflight_check_path))
            acceptance_bundle = build_acceptance_decision(
                cycle_id=normalized_cycle_id,
                run_id=normalized_cycle_id,
                preflight_check=preflight_check,
                postflight_check=postflight_check,
                reliability_metrics=_load_json_safe(reliability_metrics_path),
                incident_index=_load_json_safe(incident_index_path),
                output_dir=cycle_dir,
            )
            acceptance_decision = _as_dict(acceptance_bundle.get("acceptance_decision"))
            acceptance_decision_path = Path(str(acceptance_bundle.get("acceptance_decision_json") or acceptance_decision_path))
        except Exception as integrity_exc:
            logger.warning(
                "ops_hardening_acceptance_stage_failed",
                extra={"cycle_id": normalized_cycle_id, "error": str(integrity_exc)},
            )
            artifact_index = {"cycle_id": normalized_cycle_id, "run_id": normalized_cycle_id, "overall_status": "missing_artifacts", "missing_artifacts": ["acceptance_stage_failed"], "path_pollution": [], "artifacts": []}
            consistency_report = {"cycle_id": normalized_cycle_id, "run_id": normalized_cycle_id, "overall_status": "fail", "checks": [], "mismatches": [str(integrity_exc)]}
            postflight_check = {"cycle_id": normalized_cycle_id, "run_id": normalized_cycle_id, "overall_status": "blocked", "blocking_issues": [str(integrity_exc)], "warnings": [], "next_actions": ["retry_postflight"]}
            acceptance_decision = {"cycle_id": normalized_cycle_id, "run_id": normalized_cycle_id, "decision": "hold", "accepted": False, "reasons": [str(integrity_exc)], "required_actions": ["retry_acceptance"]}
            write_json_crash_safe(artifact_index_path, artifact_index)
            write_json_crash_safe(consistency_report_path, consistency_report)
            write_json_crash_safe(postflight_check_path, postflight_check)
            write_json_crash_safe(acceptance_decision_path, acceptance_decision)
        write_json_crash_safe(preflight_check_path, preflight_check)
        write_json_crash_safe(evidence_integrity_report_path, evidence_integrity_report)
        launch_checklist_path.write_text(
            json.dumps(launch_checklist, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        readiness_dashboard_path.write_text(
            json.dumps(readiness_dashboard, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        launch_verdict_path.write_text(
            json.dumps(launch_verdict, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        broker_safety_gate_path.write_text(
            json.dumps(broker_safety_gate, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        launch_batch_path.write_text(
            json.dumps(launch_batch, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ops_summary_path.write_text(
            json.dumps(ops_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Recompile next-cycle policy with execution feedback so task patches reflect shadow/paper outcomes.
        cycle_policy = persist_next_cycle_policy(
            evaluation,
            output_dir=cycle_dir,
            cycle_id=normalized_cycle_id,
            task_outcome_index=task_outcome_index,
            task_schedule_plan=task_schedule_plan,
            task_retry_report=retry_report,
            task_manifest=task_manifest,
            execution_feedback=execution_feedback,
            candidate_lifecycle_index=candidate_lifecycle_index,
        )
        cycle_state = cycle_state_store.complete_cycle(
            cycle_state,
            detail={
                "rerun": bool(rerun),
                "resume": bool(resume),
                "candidate_count": len(candidate_results),
            },
        )
        rerun_report_path.write_text(json.dumps(rerun_report, indent=2, ensure_ascii=False), encoding="utf-8")
        resume_report_path.write_text(json.dumps(resume_report, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "cycle_id": normalized_cycle_id,
            "cycle_dir": str(cycle_dir),
            "experiment_specs": normalized_specs,
            "candidate_results": candidate_results,
            "evaluation": evaluation,
            "next_cycle_policy": cycle_policy["policy"],
            "artifacts": {
                "cycle_spec_json": str(cycle_dir / "cycle_spec.json"),
                "task_manifest_json": str(task_manifest_path),
                "task_execution_log_jsonl": str(task_execution_log_path),
                "task_outcome_index_json": str(task_outcome_index_path),
                "task_schedule_plan_json": str(task_schedule_plan_path),
                "task_budget_summary_json": str(task_budget_summary_path),
                "task_retry_report_json": str(task_retry_report_path),
                "cycle_state_json": str(cycle_state_path),
                "stage_history_jsonl": str(stage_history_path),
                "rerun_report_json": str(rerun_report_path),
                "resume_report_json": str(resume_report_path),
                "agent_reports_dir": str(agent_reports_dir),
                "experiments_dir": str(experiments_dir),
                "agent_tasks_dir": str(cycle_dir / "agent_tasks"),
                "agent_tasks_json": str(cycle_dir / "agent_tasks" / "tasks.json"),
                "agent_tasks_stocks_json": str(cycle_dir / "agent_tasks" / "stocks_scalp.json"),
                "agent_tasks_options_json": str(cycle_dir / "agent_tasks" / "options_scalp.json"),
                "agent_tasks_ml_brain_json": str(cycle_dir / "agent_tasks" / "ml_brain.json"),
                "agent_tasks_evaluator_json": str(cycle_dir / "agent_tasks" / "evaluator.json"),
                "leaderboard_json": evaluation["artifacts"]["leaderboard_json"],
                "promotion_decision_json": evaluation["artifacts"]["promotion_decision_json"],
                "portfolio_evaluation_json": evaluation["artifacts"]["portfolio_evaluation_json"],
                "blocked_summary_json": evaluation["artifacts"].get("blocked_summary_json"),
                "quality_gate_breakdown_json": evaluation["artifacts"].get("quality_gate_breakdown_json"),
                "next_cycle_recommendations_json": evaluation["artifacts"].get("next_cycle_recommendations_json"),
                "next_cycle_policy_json": cycle_policy["path"],
                "feedback_mapping_report_json": cycle_policy["feedback_mapping_report_path"],
                "candidate_pool_json": candidate_pool_artifacts["candidate_pool_json"],
                "lineage_index_json": candidate_pool_artifacts["lineage_index_json"],
                "portfolio_selection_json": portfolio_selection_path,
                "allocation_plan_json": allocation_plan_path,
                "promotion_gate_json": promotion_gate_path,
                "shadow_candidates_dir": shadow_bundle["shadow_candidates_dir"],
                "paper_candidates_dir": paper_bridge["paper_candidates_dir"],
                "operator_controls_json": str(operator_controls_path),
                "live_candidates_dir": live_bridge["live_candidates_dir"],
                "capital_governor_report_json": str(capital_governor_report_path),
                "risk_guard_report_json": str(risk_guard_report_path),
                "session_control_report_json": str(session_control_report_path),
                "operator_audit_log_jsonl": str(operator_audit_log_path),
                "control_decision_index_json": str(control_decision_index_path),
                "live_runs_dir": live_execution["live_runs_dir"],
                "broker_session_json": str(broker_pipeline["broker_session_json"]),
                "broker_profile_report_json": str(broker_pipeline["broker_profile_report_json"]),
                "broker_binding_report_json": str(broker_pipeline["broker_binding_report_json"]),
                "real_broker_dry_run_json": str(broker_pipeline["real_broker_dry_run_json"]),
                "broker_safety_gate_json": str(broker_pipeline["broker_safety_gate_json"]),
                "broker_launch_switch_json": str(broker_pipeline["broker_launch_switch_json"]),
                "broker_readonly_probe_json": str(broker_pipeline["broker_readonly_probe_json"]),
                "broker_sandbox_orders_json": str(broker_pipeline["broker_sandbox_orders_json"]),
                "broker_sandbox_events_jsonl": str(broker_pipeline["broker_sandbox_events_jsonl"]),
                "broker_reconcile_report_json": str(broker_pipeline["broker_reconcile_report_json"]),
                "eod_closeout_report_json": str(broker_pipeline["eod_closeout_report_json"]),
                "broker_recovery_report_json": str(broker_pipeline["broker_recovery_report_json"]),
                "broker_launch_barrier_json": str(broker_pipeline["broker_launch_barrier_json"]),
                "canary_launch_gate_json": str(broker_pipeline["canary_launch_gate_json"]),
                "manual_approval_report_json": str(broker_pipeline["manual_approval_report_json"]),
                "canary_run_summary_json": str(broker_pipeline["canary_run_summary_json"]),
                "intraday_monitor_report_json": str(broker_pipeline["intraday_monitor_report_json"]),
                "post_trade_attribution_json": str(broker_pipeline["post_trade_attribution_json"]),
                "canary_feedback_report_json": str(broker_pipeline["canary_feedback_report_json"]),
                "live_batch_plan_json": str(broker_pipeline["live_batch_plan_json"]),
                "live_batch_status_json": str(broker_pipeline["live_batch_status_json"]),
                "pre_open_prep_report_json": str(broker_pipeline["pre_open_prep_report_json"]),
                "intraday_batch_report_json": str(broker_pipeline["intraday_batch_report_json"]),
                "eod_batch_close_report_json": str(broker_pipeline["eod_batch_close_report_json"]),
                "live_ops_summary_json": str(broker_pipeline["live_ops_summary_json"]),
                "live_batch_feedback_report_json": str(broker_pipeline["live_batch_feedback_report_json"]),
                "trading_day_status_json": str(trading_day_status_path),
                "ops_control_center_json": str(ops_control_center_path),
                "end_of_day_review_json": str(end_of_day_review_path),
                "weekly_candidate_review_json": str(weekly_candidate_review_path),
                "review_plan_patch_json": str(review_plan_patch_path),
                "human_ops_summary_json": str(human_ops_summary_path),
                "multi_day_run_plan_json": str(multi_day_run_plan_path),
                "multi_day_run_report_json": str(multi_day_run_report_path),
                "incident_log_jsonl": str(incident_log_path),
                "incident_index_json": str(incident_index_path),
                "recovery_coordinator_report_json": str(recovery_coordinator_report_path),
                "operator_shift_summary_json": str(operator_shift_summary_path),
                "reliability_metrics_json": str(reliability_metrics_path),
                "ops_registry_json": str(ops_registry_path),
                "orders_state_json": str(broker_pipeline["orders_state_json"]),
                "orders_events_jsonl": str(broker_pipeline["orders_events_jsonl"]),
                "account_snapshot_json": str(broker_pipeline["account_snapshot_json"]),
                "positions_snapshot_json": str(broker_pipeline["positions_snapshot_json"]),
                "position_reconcile_report_json": str(broker_pipeline["position_reconcile_report_json"]),
                "live_risk_report_json": str(broker_pipeline["live_risk_report_json"]),
                "execution_reconcile_report_json": str(broker_pipeline["execution_reconcile_report_json"]),
                "kill_switch_report_json": str(broker_pipeline["kill_switch_report_json"]),
                "recovery_report_json": str(broker_pipeline["recovery_report_json"]),
                "live_capability_inventory_json": str(reuse_audit_paths["live_capability_inventory_json"]),
                "live_reuse_plan_json": str(reuse_audit_paths["live_reuse_plan_json"]),
                "live_integration_report_json": str(reuse_audit_paths["live_integration_report_json"]),
                "live_gap_list_json": str(reuse_audit_paths["live_gap_list_json"]),
                "live_feedback_json": live_feedback_artifacts["live_feedback_json"],
                "shadow_runs_dir": shadow_runs["shadow_runs_dir"],
                "execution_feedback_json": execution_feedback_artifacts["execution_feedback_json"],
                "live_feedback_index_json": live_feedback_artifacts["live_feedback_index_json"],
                "execution_feedback_report_json": str(execution_feedback_report_path),
                "candidate_lifecycle_index_json": str(candidate_lifecycle_index_path),
                "candidate_gate_report_json": str(candidate_gate_report_path),
                "candidate_retention_report_json": str(candidate_retention_report_path),
                "candidate_lifecycle_report_json": str(candidate_lifecycle_report_path),
                "admission_gate_json": str(admission_gate_path),
                "health_monitor_report_json": str(health_monitor_report_path),
                "auto_halt_report_json": str(auto_halt_report_path),
                "active_registry_json": str(active_registry_path),
                "active_registry_report_json": str(active_registry_report_path),
                "preflight_check_json": str(preflight_check_path),
                "postflight_check_json": str(postflight_check_path),
                "acceptance_decision_json": str(acceptance_decision_path),
                "artifact_index_json": str(artifact_index_path),
                "consistency_report_json": str(consistency_report_path),
                "launch_checklist_json": str(launch_checklist_path),
                "readiness_dashboard_json": str(readiness_dashboard_path),
                "launch_verdict_json": str(launch_verdict_path),
                "evidence_integrity_report_json": str(evidence_integrity_report_path),
                "launch_batch_json": str(launch_batch_path),
                "ops_summary_json": str(ops_summary_path),
                "policy_compatibility_summary_json": str(policy_compatibility_summary_path),
                "policy_observability_consumers_json": str(policy_observability_consumers_path),
                "launch_registry_json": str(launch_registry_path),
                "system_status_json": str(system_status_path),
                "evaluation_bundle_json": evaluation["artifacts"]["evaluation_bundle_json"],
            },
        }


def _normalize_cycle_id(cycle_id: str | None) -> str:
    value = str(cycle_id or "").strip()
    if value:
        return value
    return datetime.now(timezone.utc).date().isoformat()


def _write_agent_reports(
    *,
    agent_reports_dir: Path,
    agent_plan: Dict[str, Any] | None,
    state_snapshot: Dict[str, Any] | None,
    policy_snapshot: Dict[str, Any] | None,
    execution_reports: List[Dict[str, Any]],
) -> None:
    payloads = {
        "plan.json": agent_plan or {},
        "data_readiness.json": _extract_data_readiness(agent_plan),
        "state_snapshot.json": state_snapshot or {},
        "policy_snapshot.json": policy_snapshot or {},
        "execution_reports.json": execution_reports,
    }
    for name, payload in payloads.items():
        (agent_reports_dir / name).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )


def _extract_data_readiness(agent_plan: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(agent_plan, dict):
        return {}
    data_readiness = agent_plan.get("data_readiness")
    if isinstance(data_readiness, dict):
        return data_readiness
    return {}
