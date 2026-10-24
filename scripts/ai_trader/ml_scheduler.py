#!/usr/bin/env python3
"""
ML Job Scheduler - Single Slot with Priority Queue

Features:
- Single slot execution (no parallel ML jobs)
- Priority queue (oof > alpha_loop > lstm)
- Event-driven job completion → Brain review → next job
- Memory guard integration
- Job queue persistence (JSONL + Kafka fallback)
- Time window enforcement (weekday/weekend schedules)

Usage:
    from scripts.ai_trader.ml_scheduler import MLScheduler, enqueue_job

    scheduler = MLScheduler()
    await scheduler.process_queue()  # Main loop

    # Or enqueue a job
    enqueue_job({
        "job_type": "oof",
        "priority": 100,
        "params": {"symbols": ["AAPL"], "window_days": 30}
    })
"""
import sys
from pathlib import Path

# Ensure project root is on sys.path so that "libs" and "scripts.*" imports work
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import asyncio
import importlib
import json
import os
import shlex
import subprocess
from pathlib import Path
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import json as _json
from libs.utils.market_calendar import is_market_closed
import json

from libs.logger import get_logger
from libs.config import config as _cfg
from libs.event_bus import publish
from libs.health import Action, make_action_id # New imports needed for Action object
from libs.db.clickhouse import ClickHouseDB
from libs.utils.market_calendar import is_market_closed
from scripts.ai_trader.research import experiment_api
from scripts.ai_trader.research.robustness import run_default_robustness

logger = get_logger("ai_trader.ml_scheduler")


# ============================================================================ 
# Job Queue Management
# ============================================================================ 

def _runs_dir() -> Path:
    """Get runs directory for today."""
    return Path("runs/ai_trader") / date.today().isoformat()



def _queue_file() -> Path:
    """Get persistent queue file path."""
    queue_dir = Path("runs/ai_trader/queue")
    queue_dir.mkdir(parents=True, exist_ok=True)
    return queue_dir / "ml_jobs.jsonl"


def _slot_lock_file() -> Path:
    """Get ML slot lock file."""
    locks_dir = _runs_dir() / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / "ml_slot.lock"


def _scheduler_pid_file() -> Path:
    """PID file for scheduler singleton visibility and watchdogs."""
    pids_dir = _runs_dir() / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    return pids_dir / "ml_scheduler.pid"


def _acquire_singleton_lock() -> bool:
    """Acquire singleton lock with stale lock cleanup.

    Ensures only one ml_scheduler instance runs at a time.
    Checks if another ml_scheduler process is actually running.
    If lock exists but process is dead, removes stale lock and proceeds.

    Returns:
        True if lock acquired, False if another instance is running
    """
    import time
    import subprocess

    try:
        lock_file = _runs_dir() / "locks" / "ml_scheduler.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()

        if lock_file.exists():
            # Check if process is actually running
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "ml_scheduler.py"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Process is running - respect the lock
                    logger.info("ml_scheduler_already_running", lock_age_sec=int(now - lock_file.stat().st_mtime))
                    return False
                else:
                    # Lock exists but no process - stale lock, clean it up
                    logger.warning("stale_scheduler_lock_detected", lock_age_sec=int(now - lock_file.stat().st_mtime))
                    lock_file.unlink()
            except Exception as e:
                # If we can't check process, be conservative and respect the lock
                logger.warning("scheduler_process_check_failed", error=str(e))
                return False

        # No lock or stale lock cleaned up - acquire it
        lock_file.write_text(str(int(now)), encoding="utf-8")
        logger.info("scheduler_singleton_lock_acquired", lock_file=str(lock_file))
        return True

    except Exception as e:
        logger.error("scheduler_lock_acquisition_failed", error=str(e), exc_info=True)
        # On error, be conservative and allow to run
        return True


def _acquire_slot() -> bool:
    """Try to acquire ML slot lock.

    Returns:
        True if acquired, False if slot is busy
    """
    lock_file = _slot_lock_file()
    if lock_file.exists():
        try:
            # Check if lock is stale (>6 hours)
            age = time.time() - lock_file.stat().st_mtime
            if age < 6 * 3600:  # 6 hours
                return False
            # Stale lock, remove it
            lock_file.unlink()
            logger.warning("removed_stale_ml_slot_lock", age_hours=age/3600)
        except Exception as e:
            logger.error("slot_lock_check_failed", error=str(e))
            return False

    # Acquire lock
    try:
        lock_file.write_text(str(int(time.time())), encoding="utf-8")
        return True
    except Exception as e:
        logger.error("slot_lock_acquire_failed", error=str(e))
        return False


def _release_slot():
    """Release ML slot lock."""
    try:
        lock_file = _slot_lock_file()
        if lock_file.exists():
            lock_file.unlink()
            logger.info("ml_slot_released")
    except Exception as e:
        logger.error("slot_release_failed", error=str(e))


def enqueue_job(job: Dict[str, Any]) -> str:
    """Add a job to the queue.

    Args:
        job: Job specification with keys:
            - job_type: "oof", "alpha_loop", or "lstm"
            - priority: int (higher = more urgent)
            - params: dict of job parameters
            - deadline: optional ISO timestamp
            - est_mem_gb: estimated memory (for guard)

    Returns:
        job_id
    """
    # Generate job_id
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    import random, string
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    job_id = f"{ts}-{rand}"

    # Enrich job
    job["job_id"] = job_id
    job["enqueued_at"] = datetime.now().isoformat()
    job["status"] = "queued"

    # Set defaults
    job.setdefault("priority", _default_priority(job["job_type"]))
    job.setdefault("est_mem_gb", _default_memory(job["job_type"]))

    # Append to queue file
    queue_file = _queue_file()
    try:
        with queue_file.open("a") as f:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")
        logger.info("job_enqueued", job_id=job_id, job_type=job["job_type"], priority=job["priority"])
    except Exception as e:
        logger.error("enqueue_failed", job_id=job_id, error=str(e))
        raise

    return job_id


def _default_priority(job_type: str) -> int:
    """Get default priority for job type."""
    priorities = {
        "oof": 100,
        "alpha_loop": 80,
        "lstm": 60,
        "hpo_cli": 75,
        "strategy_experiment": 78,
        "bulk_backfill": 70,
        "feature_enhanced_backfill": 65,
    }
    return priorities.get(job_type, 50)


def _default_memory(job_type: str) -> float:
    """Estimate memory requirement for job type."""
    estimates = {
        "oof": 4.0,      # XGBoost small sample
        "alpha_loop": 4.0,  # Single process HPO (reduced from 8.0 to fit 7GB systems)
        "lstm": 12.0,     # LSTM training
        "hpo_cli": 6.0,
        "strategy_experiment": 6.0,
        "feature_enhanced_backfill": 6.0,
    }
    return estimates.get(job_type, 4.0)


_STRATEGY_EXPERIMENT_ADAPTERS: dict[str, tuple[str, str]] = {
    "stocks_kernel_backtest": ("services.backtest.stocks_kernel_adapter", "run_strategy_experiment"),
    "options_single_leg_replay": ("services.option_replay.research_adapter", "run_strategy_experiment"),
}


def _json_safe(data: Any) -> Any:
    """Best-effort JSON-safe conversion."""
    try:
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))
    except Exception:
        return str(data)


def _strategy_experiment_root(experiment_id: str) -> Path:
    root = _runs_dir() / "strategy_experiments" / str(experiment_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _strategy_experiment_has_runner_hint(spec: Dict[str, Any]) -> bool:
    engine = spec.get("engine") if isinstance(spec.get("engine"), dict) else {}
    if any(spec.get(k) for k in ("runner", "runner_callable", "adapter_callable", "command", "script", "run_cmd")):
        return True
    adapter = str(spec.get("adapter") or engine.get("adapter") or "").strip()
    if adapter in _STRATEGY_EXPERIMENT_ADAPTERS:
        return True
    return False


def _normalize_strategy_experiment_spec(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a strategy_experiment payload into a single spec dict."""
    job_params = params.get("params", params) if isinstance(params.get("params"), dict) else params
    spec: Dict[str, Any] = {}
    for key in ("spec", "experiment", "strategy_experiment"):
        nested = job_params.get(key)
        if isinstance(nested, dict):
            spec.update(nested)
    for key, value in job_params.items():
        if key not in {"spec", "experiment", "strategy_experiment"}:
            spec.setdefault(key, value)

    engine = spec.get("engine") if isinstance(spec.get("engine"), dict) else {}
    strategy_ref = spec.get("strategy_ref") if isinstance(spec.get("strategy_ref"), dict) else {}
    module_selection = spec.get("module_selection") if isinstance(spec.get("module_selection"), dict) else {}
    robustness = spec.get("robustness") if isinstance(spec.get("robustness"), dict) else {}
    execution_reality = spec.get("execution_reality") if isinstance(spec.get("execution_reality"), dict) else {}
    sizing = spec.get("sizing") if isinstance(spec.get("sizing"), dict) else {}
    lineage = spec.get("lineage") if isinstance(spec.get("lineage"), dict) else {}
    acceptance = spec.get("acceptance") if isinstance(spec.get("acceptance"), dict) else {}
    cycle_id = str(
        spec.get("cycle_id")
        or job_params.get("cycle_id")
        or datetime.now().date().isoformat()
    )
    adapter = str(
        spec.get("adapter")
        or engine.get("adapter")
        or job_params.get("adapter")
        or ""
    ).strip()

    raw_symbols = spec.get("symbols") or job_params.get("symbols") or []
    if isinstance(raw_symbols, (list, tuple, set)):
        symbols = list(raw_symbols)
    elif raw_symbols:
        symbols = [raw_symbols]
    else:
        symbols = []

    engine_mode = str(engine.get("mode") or job_params.get("mode") or "backtest")
    normalized_engine = dict(engine)
    normalized_engine["adapter"] = adapter
    normalized_engine["mode"] = engine_mode

    stable_id_spec = {
        "cycle_id": cycle_id,
        "market": str(spec.get("market") or job_params.get("market") or "").strip().lower(),
        "strategy_family": str(spec.get("strategy_family") or job_params.get("strategy_family") or "").strip(),
        "engine": normalized_engine,
        "universe": {
            "symbols": symbols,
        },
        "strategy_ref": {
            "template_id": strategy_ref.get("template_id"),
            "definition_id": strategy_ref.get("definition_id"),
        },
        "module_selection": module_selection,
        "parameters": spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {
            k: v for k, v in job_params.items() if k not in {
                "spec",
                "experiment",
                "strategy_experiment",
                "engine",
                "strategy_ref",
                "module_selection",
                "robustness",
                "execution_reality",
                "sizing",
                "lineage",
                "acceptance",
                "universe",
                "runner",
                "runner_callable",
                "adapter_callable",
                "command",
                "run_cmd",
                "script",
                "robustness_hook",
            }
        },
        "data_window": spec.get("data_window") if isinstance(spec.get("data_window"), dict) else job_params.get("data_window", {}),
        "labels_and_ml": spec.get("labels_and_ml") if isinstance(spec.get("labels_and_ml"), dict) else job_params.get("labels_and_ml", {}),
        "lineage": lineage,
    }

    experiment_id = str(
        spec.get("experiment_id")
        or job_params.get("experiment_id")
        or job_params.get("task_id")
        or experiment_api.stable_experiment_id(stable_id_spec)
    )

    normalized = {
        "experiment_id": experiment_id,
        "cycle_id": cycle_id,
        "market": str(spec.get("market") or job_params.get("market") or "").strip().lower(),
        "strategy_family": str(spec.get("strategy_family") or job_params.get("strategy_family") or "").strip(),
        "engine": normalized_engine,
        "universe": spec.get("universe") if isinstance(spec.get("universe"), dict) else {
            "symbols": symbols,
        },
        "strategy_ref": {
            "template_id": strategy_ref.get("template_id"),
            "definition_id": strategy_ref.get("definition_id"),
        },
        "module_selection": module_selection,
        "parameters": spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {
            k: v for k, v in job_params.items() if k not in {
                "spec",
                "experiment",
                "strategy_experiment",
                "engine",
                "strategy_ref",
                "module_selection",
                "robustness",
                "execution_reality",
                "sizing",
                "lineage",
                "acceptance",
                "universe",
                "runner",
                "runner_callable",
                "adapter_callable",
                "command",
                "run_cmd",
                "script",
                "robustness_hook",
            }
        },
        "data_window": spec.get("data_window") if isinstance(spec.get("data_window"), dict) else job_params.get("data_window", {}),
        "labels_and_ml": spec.get("labels_and_ml") if isinstance(spec.get("labels_and_ml"), dict) else job_params.get("labels_and_ml", {}),
        "robustness": robustness,
        "execution_reality": execution_reality,
        "sizing": sizing,
        "lineage": lineage,
        "acceptance": acceptance,
        "target": spec.get("target") or job_params.get("target"),
        "command": spec.get("command") or job_params.get("command"),
        "runner": spec.get("runner") or job_params.get("runner"),
        "robustness_hook": spec.get("robustness_hook") or job_params.get("robustness_hook"),
    }
    normalized["engine"]["adapter"] = adapter or normalized["engine"]["adapter"] or normalized["strategy_ref"].get("template_id") or normalized["strategy_ref"].get("definition_id") or ""
    normalized.setdefault("adapter", normalized["engine"]["adapter"])
    normalized.setdefault("status", "pending")
    return normalized


def _validate_strategy_experiment_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    issues: list[str] = []
    if not spec.get("market"):
        issues.append("missing_market")
    if not (spec.get("strategy_family") or (spec.get("strategy_ref") or {}).get("template_id") or (spec.get("strategy_ref") or {}).get("definition_id")):
        issues.append("missing_strategy_identity")
    if not _strategy_experiment_has_runner_hint(spec):
        issues.append("missing_runner_hint")
    if not spec.get("experiment_id"):
        issues.append("missing_experiment_id")
    return {
        "ok": not issues,
        "issues": issues,
        "spec": spec,
    }


def _resolve_strategy_experiment_callable(spec: Dict[str, Any]) -> Any:
    """Resolve an executable strategy experiment runner if one is available."""
    for key in ("runner", "runner_callable", "adapter_callable"):
        ref = spec.get(key)
        if callable(ref):
            return ref
        if isinstance(ref, str) and ":" in ref:
            module_name, attr_name = ref.split(":", 1)
            module = importlib.import_module(module_name)
            return getattr(module, attr_name)

    adapter = str(spec.get("adapter") or spec.get("engine", {}).get("adapter") or "").strip()
    adapter_module = _STRATEGY_EXPERIMENT_ADAPTERS.get(adapter)
    if adapter_module:
        module_name, attr_name = adapter_module
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)

    command = spec.get("command") or spec.get("run_cmd")
    if command:
        return command

    script = spec.get("script")
    if script:
        return script

    return None


def _normalize_strategy_runner_result(raw_result: Any) -> Dict[str, Any]:
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, tuple) and len(raw_result) == 2:
        ok, payload = raw_result
        if isinstance(payload, dict):
            payload = dict(payload)
        else:
            payload = {"result": payload}
        payload.setdefault("ok", bool(ok))
        return payload
    return {"ok": bool(raw_result), "result": raw_result}


def _run_strategy_experiment_adapter(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the experiment using an adapter/callable/command if available."""
    try:
        resolved = _resolve_strategy_experiment_callable(spec)
    except Exception as exc:
        return {
            "ok": False,
            "status": "FAILED",
            "error": f"strategy_runner_resolution_failed: {exc}",
            "artifacts": [],
            "metrics": {},
        }

    if resolved is None:
        return {
            "ok": False,
            "status": "BLOCKED",
            "error": "missing_strategy_experiment_runner",
            "artifacts": [],
            "metrics": {},
        }

    try:
        if callable(resolved):
            raw = resolved(spec)
            return _normalize_strategy_runner_result(raw)

        if isinstance(resolved, str):
            candidate = resolved.strip()
            if candidate.endswith(".py") or Path(candidate).exists():
                cmd = [sys.executable, candidate]
            else:
                cmd = shlex.split(candidate)
        elif isinstance(resolved, (list, tuple)):
            cmd = [str(part) for part in resolved]
        else:
            cmd = shlex.split(str(resolved))

        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
        payload: Dict[str, Any] = {
            "ok": cp.returncode == 0,
            "returncode": cp.returncode,
            "stdout": (cp.stdout or "")[-2000:],
            "stderr": (cp.stderr or "")[-2000:],
            "artifacts": [],
            "metrics": {},
        }
        try:
            if cp.stdout:
                parsed = json.loads(cp.stdout)
                if isinstance(parsed, dict):
                    payload["run_output"] = parsed
        except Exception:
            pass
        return payload
    except Exception as exc:
        return {
            "ok": False,
            "status": "FAILED",
            "error": str(exc),
            "artifacts": [],
            "metrics": {},
        }


def _run_strategy_experiment_robustness_hook(spec: Dict[str, Any], run_result: Dict[str, Any]) -> Dict[str, Any]:
    hook = spec.get("robustness_hook")
    if not hook:
        return run_default_robustness(spec, run_result)

    try:
        if callable(hook):
            raw = hook(spec, run_result)
        elif isinstance(hook, str) and ":" in hook:
            module_name, attr_name = hook.split(":", 1)
            module = importlib.import_module(module_name)
            raw = getattr(module, attr_name)(spec, run_result)
        else:
            raw = hook
        if isinstance(raw, dict):
            return run_default_robustness(spec, raw)
        return run_default_robustness(spec, {"result": raw})
    except Exception as exc:
        return {
            "ok": False,
            "status": "FAILED",
            "error": f"robustness_hook_failed: {exc}",
            "metrics": {},
            "artifacts": [],
        }


def _extract_summary_metrics(run_result: Dict[str, Any]) -> Dict[str, Any]:
    metrics = {}
    for key in ("metrics", "summary_metrics", "result_metrics"):
        value = run_result.get(key)
        if isinstance(value, dict):
            metrics.update(value)
    for key in ("trades", "win_rate", "net_pnl", "pnl", "sharpe", "max_drawdown_pct", "max_drawdown", "profit_factor", "coverage", "mdd"):
        if key in run_result and run_result.get(key) is not None:
            metrics.setdefault(key, run_result.get(key))
    if "max_drawdown" in metrics and "max_drawdown_pct" not in metrics:
        metrics["max_drawdown_pct"] = metrics["max_drawdown"]
    if "max_drawdown_pct" in metrics and "mdd" not in metrics:
        metrics["mdd"] = metrics["max_drawdown_pct"]
    if "net_pnl" not in metrics and "pnl" in metrics:
        metrics["net_pnl"] = metrics["pnl"]
    return metrics


def _build_strategy_experiment_result(
    *,
    spec: Dict[str, Any],
    validation: Dict[str, Any],
    run_result: Dict[str, Any],
    robustness_result: Dict[str, Any],
    job_id: str,
) -> Dict[str, Any]:
    summary_metrics = _extract_summary_metrics(run_result)
    run_status = str(run_result.get("status") or "").strip().lower()
    if "ok" in run_result:
        run_ok = bool(run_result.get("ok"))
    else:
        run_ok = run_status in {"completed", "ok", "success", "passed"}
    execution_reality = {}
    if isinstance(run_result.get("execution_reality"), dict):
        execution_reality.update(run_result["execution_reality"])
    elif isinstance(run_result.get("execution_audit"), dict):
        execution_reality.update(run_result["execution_audit"])
    if isinstance(spec.get("execution_reality"), dict):
        execution_reality = {**spec["execution_reality"], **execution_reality}

    quality_flags = dict(run_result.get("quality_flags") or {})
    quality_flags.setdefault("data_ok", run_ok)
    quality_flags["enough_trades"] = bool(summary_metrics.get("trades", 0) or 0) >= int((spec.get("acceptance") or {}).get("min_trades", 0) or 0)
    robustness_pass = bool(robustness_result.get("ok", robustness_result.get("passed", True)))
    quality_flags["robustness_pass"] = robustness_pass

    acceptance = spec.get("acceptance") if isinstance(spec.get("acceptance"), dict) else {}
    max_drawdown_limit = acceptance.get("max_drawdown_pct")
    max_drawdown = summary_metrics.get("max_drawdown_pct", summary_metrics.get("mdd"))
    if max_drawdown_limit is not None and max_drawdown is not None:
        quality_flags["passed_acceptance"] = bool(quality_flags["enough_trades"]) and float(max_drawdown) <= float(max_drawdown_limit)
    else:
        quality_flags["passed_acceptance"] = bool(quality_flags["enough_trades"])

    status = "completed"
    notes: list[str] = []
    if not validation.get("ok", False):
        status = "blocked"
        notes.extend(validation.get("issues") or [])
    elif not run_ok:
        status = str(run_result.get("status") or "failed").lower()
        if status not in {"blocked", "failed", "completed"}:
            status = "failed"
        error_note = run_result.get("error")
        if not error_note:
            run_notes = run_result.get("notes")
            if isinstance(run_notes, list) and run_notes:
                error_note = str(run_notes[0])
        notes.append(error_note or "strategy_runner_failed")
    elif not robustness_pass:
        status = "blocked"
        notes.append(robustness_result.get("error") or "robustness_failed")
    elif not quality_flags.get("passed_acceptance", False):
        status = "blocked"
        notes.append("acceptance_gate_failed")

    if status == "completed":
        ok = True
    else:
        ok = False

    experiment_root = _strategy_experiment_root(str(spec.get("experiment_id") or job_id))
    result_path = experiment_root / "experiment_result.json"
    artifacts = list(run_result.get("artifacts") or [])
    artifacts = [str(a) for a in artifacts if a]
    if str(result_path) not in artifacts:
        artifacts.append(str(result_path))

    experiment_result = {
        "experiment_id": str(spec.get("experiment_id") or job_id),
        "cycle_id": spec.get("cycle_id"),
        "status": status,
        "market": spec.get("market"),
        "strategy_family": spec.get("strategy_family"),
        "adapter": spec.get("engine", {}).get("adapter") or spec.get("adapter"),
        "summary_metrics": summary_metrics,
        "quality_flags": quality_flags,
        "robustness": robustness_result,
        "execution_reality": execution_reality,
        "sizing": spec.get("sizing") or {},
        "lineage": spec.get("lineage") or {},
        "artifacts": {
            "result_json": str(result_path),
            "run_artifacts": [str(a) for a in run_result.get("artifacts") or []],
        },
        "notes": notes,
        "validation": validation,
    }
    result_path.write_text(json.dumps(_json_safe(experiment_result), indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "ok": ok,
        "status": status,
        "experiment_result": experiment_result,
        "result_path": str(result_path),
        "summary_metrics": summary_metrics,
        "quality_flags": quality_flags,
        "execution_reality": execution_reality,
        "robustness": robustness_result,
        "notes": notes,
        "artifacts": artifacts,
        "metrics": {
            **summary_metrics,
            "status": status,
            "robustness_pass": robustness_pass,
            "passed_acceptance": quality_flags.get("passed_acceptance", False),
            "enough_trades": quality_flags["enough_trades"],
            "data_ok": quality_flags["data_ok"],
            "experiment_id": str(spec.get("experiment_id") or job_id),
            "adapter": spec.get("engine", {}).get("adapter") or spec.get("adapter"),
        },
        "error": None if ok else "; ".join(notes) if notes else status,
    }


def _persist_strategy_experiment_tracking(
    *,
    job_id: str,
    spec: Dict[str, Any],
    validation: Dict[str, Any],
    run_result: Dict[str, Any],
    robustness_result: Dict[str, Any],
    final_result: Dict[str, Any],
) -> None:
    experiment_id = str(spec.get("experiment_id") or job_id)
    payload = {
        "experiment_id": experiment_id,
        "job_id": job_id,
        "spec": _json_safe(spec),
        "validation": _json_safe(validation),
        "run_result": _json_safe(run_result),
        "robustness": _json_safe(robustness_result),
        "result": _json_safe(final_result.get("experiment_result")),
        "status": final_result.get("status"),
        "ok": final_result.get("ok"),
    }
    try:
        experiment_api.run_experiment(
            experiment_id,
            {
                "stage": "validate",
                "job_id": job_id,
                "validation": _json_safe(validation),
            },
        )
    except Exception:
        pass
    try:
        experiment_api.run_experiment(
            experiment_id,
            {
                "stage": "run",
                "job_id": job_id,
                "run_result": _json_safe(run_result),
            },
        )
    except Exception:
        pass
    try:
        experiment_api.run_experiment(
            experiment_id,
            {
                "stage": "robustness",
                "job_id": job_id,
                "robustness": _json_safe(robustness_result),
            },
        )
    except Exception:
        pass
    try:
        experiment_api.run_experiment(
            experiment_id,
            {
                "stage": "summarize",
                "job_id": job_id,
                "summary_metrics": _json_safe(final_result.get("summary_metrics") or {}),
                "quality_flags": _json_safe(final_result.get("quality_flags") or {}),
                "execution_reality": _json_safe(final_result.get("execution_reality") or {}),
                "notes": _json_safe(final_result.get("notes") or []),
            },
        )
    except Exception:
        pass

    terminal_status = str(final_result.get("status") or "failed").strip().lower() or "failed"
    try:
        terminal_params = {
            "spec": _json_safe(spec),
            "cycle_id": spec.get("cycle_id"),
            "quality_flags": _json_safe(final_result.get("quality_flags") or {}),
            "robustness": _json_safe(final_result.get("robustness") or {}),
            "execution_reality": _json_safe(final_result.get("execution_reality") or {}),
            "sizing": _json_safe(final_result.get("experiment_result", {}).get("sizing") or {}),
            "lineage": _json_safe(final_result.get("experiment_result", {}).get("lineage") or {}),
            "notes": _json_safe(final_result.get("notes") or []),
        }
        terminal_metrics = _json_safe(final_result.get("metrics") or {})
        terminal_artifacts = _json_safe(final_result.get("experiment_result", {}).get("artifacts") or {})
        terminal_error = final_result.get("error") or "; ".join(final_result.get("notes") or []) or terminal_status
        if terminal_status == "blocked":
            experiment_api.block_experiment(
                experiment_id,
                error=str(terminal_error),
                metrics=terminal_metrics,
                artifacts=terminal_artifacts,
                params=terminal_params,
            )
        elif terminal_status == "failed":
            experiment_api.fail_experiment(
                experiment_id,
                error=str(terminal_error),
                metrics=terminal_metrics,
                artifacts=terminal_artifacts,
                params=terminal_params,
            )
        else:
            experiment_api.complete_experiment(
                experiment_id,
                status="COMPLETED",
                metrics=terminal_metrics,
                artifacts=terminal_artifacts,
                params=terminal_params,
            )
    except Exception:
        pass
    try:
        root = _strategy_experiment_root(experiment_id)
        track_path = root / "tracking.json"
        track_path.write_text(json.dumps(_json_safe(payload), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_queue() -> List[Dict[str, Any]]:
    """Load pending jobs from queue file.

    Returns:
        List of jobs sorted by priority (desc) then enqueued_at (asc)
    """
    queue_file = _queue_file()
    if not queue_file.exists():
        return []

    jobs = []
    try:
        with queue_file.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)
                    if job.get("status") == "queued":
                        jobs.append(job)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error("queue_load_failed", error=str(e))

    # Sort by priority (desc) then enqueued_at (asc)
    # Handle None priority values by treating them as 0
    # Handle string priorities ("high", "medium", "low") by converting to numeric
    def priority_value(p):
        if p is None:
            return 0
        if isinstance(p, (int, float)):
            return p
        # String priority mapping
        priority_map = {"high": 100, "medium": 50, "low": 10}
        return priority_map.get(str(p).lower(), 0)

    jobs.sort(key=lambda j: (-priority_value(j.get("priority")), j.get("enqueued_at", "")))

    return jobs


def mark_job_status(job_id: str, status: str):
    """Mark job status in queue file.

    Args:
        job_id: Job ID
        status: "running", "completed", "failed"
    """
    queue_file = _queue_file()
    if not queue_file.exists():
        return

    # Read all jobs
    jobs = []
    try:
        with queue_file.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)
                    if job.get("job_id") == job_id:
                        job["status"] = status
                        job[f"{status}_at"] = datetime.now().isoformat()
                    jobs.append(job)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error("queue_read_failed", error=str(e))
        return

    # Rewrite queue
    try:
        with queue_file.open("w") as f:
            for job in jobs:
                f.write(json.dumps(job, ensure_ascii=False) + "\n")
        logger.info("job_status_updated", job_id=job_id, status=status)
    except Exception as e:
        logger.error("queue_write_failed", error=str(e))


def _requeue_stale_running_jobs(max_age_sec: int = 600):
    """Requeue jobs stuck in 'running' status (zombie jobs from crashes/kills).

    Args:
        max_age_sec: Max age in seconds before a running job is considered stale (default: 600s / 10 min)
    """
    queue_file = _queue_file()
    if not queue_file.exists():
        return

    jobs = []
    requeued = []
    now = time.time()

    try:
        with queue_file.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)

                    # Check if job is stuck in running status
                    if job.get("status") == "running":
                        running_at_str = job.get("running_at")
                        if running_at_str:
                            try:
                                running_at = datetime.fromisoformat(running_at_str).timestamp()
                                age_sec = now - running_at

                                if age_sec > max_age_sec:
                                    # Requeue the stale job
                                    job["status"] = "queued"
                                    job.pop("running_at", None)
                                    requeued.append(job["job_id"])
                                    logger.warning("requeue_stale_running",
                                                 job_id=job["job_id"],
                                                 job_type=job.get("job_type"),
                                                 age_sec=int(age_sec),
                                                 reason="stale_running_status")
                            except Exception:
                                # Failed to parse running_at, skip this job
                                pass

                    jobs.append(job)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error("requeue_stale_read_failed", error=str(e))
        return

    # Rewrite queue if any jobs were requeued
    if requeued:
        try:
            with queue_file.open("w") as f:
                for job in jobs:
                    f.write(json.dumps(job, ensure_ascii=False) + "\n")
            logger.info("requeue_stale_running_complete", requeued_count=len(requeued), job_ids=requeued)
        except Exception as e:
            logger.error("requeue_stale_write_failed", error=str(e))


def _publish_ml_event(event: Dict[str, Any]) -> bool:
    """Publish ML event to Kafka with JSONL fallback and robust logging.

    Returns True if broker publish succeeded, False otherwise (file fallback still written).
    """
    # Normalize ev/beta_corr from metrics into top-level for downstream consumers
    metrics = event.get("metrics") or {}
    if "ev" in metrics:
        event["ev"] = metrics.get("ev")
    if "beta_corr" in metrics:
        event["beta_corr"] = metrics.get("beta_corr")

    ok = False
    # Resolve topic from config (fallback to internal.ml.status)
    try:
        topic = (_cfg.get("event_bus", {}).get("topics", {}) or {}).get("ml", "internal.ml.status")
    except Exception:
        topic = "internal.ml.status"
    # Try broker publish
    try:
        logger.info("ml_event_publish_start", topic=topic, event_keys=list(event.keys()), job_id=event.get("job_id"))
        res = publish(topic, event)
        logger.info("ml_event_publish_result", topic=topic, result=res, event_id=event.get("job_id"))
        ok = bool(res) if res is not None else True
        if not ok:
            logger.warning("ml_event_publish_returned_false", topic=topic, event_type=event.get("event"))
    except Exception as e:
        logger.warning("ml_event_publish_failed", error=str(e), topic=topic, exc_info=True)
        ok = False
    # Always append to local JSONL for audit/fallback (force flush to disk)
    try:
        d = _runs_dir()
        logger.info("ml_event_jsonl_path", path=str(d))
        d.mkdir(parents=True, exist_ok=True)
        f = d / "ml_events.jsonl"
        logger.info("ml_event_jsonl_file", file=str(f))
        with f.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
            fp.flush()  # Force flush buffer to OS
            os.fsync(fp.fileno())  # Force OS to write to disk
        logger.info("ml_event_jsonl_written", file=str(f), event_id=event.get("job_id"))
    except Exception as e:
        logger.error("ml_event_file_write_failed", error=str(e), exc_info=True)
    return ok


def _queue_running_jobs() -> list[dict]:
    """Return list of queue entries currently marked as running."""
    qf = _queue_file()
    running: list[dict] = []
    if not qf.exists():
        return running
    try:
        with qf.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(job.get("status", "")).lower() == "running":
                    running.append(job)
    except Exception as e:
        logger.error("queue_read_failed_for_running", error=str(e))
    return running


def _recover_orphaned_slot_lock(orphan_age_sec: int = 240) -> bool:
    """If slot lock exists but no job is running, and age exceeds threshold, remove it.

    Returns True if a lock was removed.
    """
    lf = _slot_lock_file()
    if not lf.exists():
        return False
    try:
        # If there are running jobs, do not remove
        if _queue_running_jobs():
            return False
        age = time.time() - lf.stat().st_mtime
        if age > orphan_age_sec:
            lf.unlink()
            logger.warning("removed_orphaned_slot_lock", age_sec=int(age))
            return True
    except Exception as e:
        logger.error("orphan_lock_recovery_failed", error=str(e))
    return False


# ============================================================================ 
# Job Execution
# ============================================================================ 

async def execute_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a single ML job.

    Args:
        job: Job specification

    Returns:
        Result dict with ok, duration_sec, artifacts, metrics
    """
    job_id = job["job_id"]
    job_type = job["job_type"]
    params = job.get("params", {})

    logger.info("job_execution_start", job_id=job_id, job_type=job_type, params=params)

    # Publish started event
    _publish_ml_event({
        "event": "ml.job",
        "status": "started",
        "job_id": job_id,
        "job_type": job_type,
        "params": params,
        "ts": datetime.now().isoformat()
    })

    mark_job_status(job_id, "running")
    start_time = time.time()
    result = {
        "ok": False,
        "duration_sec": 0,
        "artifacts": [],
        "metrics": {},
        "error": None
    }

    try:
        # Execute based on job type
        if job_type == "oof":
            result = await _execute_oof(job_id, params)
        elif job_type == "alpha_loop":
            result = await _execute_alpha_loop(job_id, params)
        elif job_type == "lstm":
            result = await _execute_lstm(job_id, params)
        elif job_type == "ensemble_sweep":
            result = await _execute_ensemble_sweep(job_id, params)
        elif job_type == "feature_build":
            result = await _execute_feature_build(job_id, params)
        elif job_type == "feature_eval":
            result = await _execute_feature_eval(job_id, params)
        elif job_type == "feature_apply":
            result = await _execute_feature_apply(job_id, params)
        elif job_type == "feature_search":
            result = await _execute_feature_search(job_id, params)
        elif job_type == "feature_resample":
            result = await _execute_feature_resample(job_id, params)
        elif job_type == "feature_experiment":
            result = await _execute_feature_experiment(job_id, params)
        elif job_type == "label_diagnostics":
            result = await _execute_label_diagnostics(job_id, params)
        elif job_type == "feature_promote":
            result = await _execute_feature_promote(job_id, params)
        elif job_type == "strategy_experiment":
            result = await _execute_strategy_experiment(job_id, params)
        elif job_type == "label_recompute":
            result = await _execute_label_recompute(job_id, params)
        elif job_type == "hpo_cli":
            result = await _execute_hpo_cli(job_id, params)
        elif job_type == "bulk_backfill":
            result = await _execute_bulk_backfill(job_id, params)
        elif job_type == "feature_enhanced_backfill":
            result = await _execute_feature_enhanced_backfill(job_id, params)
        elif job_type == "backfill_bars":
            normalized = dict(params or {})
            start_date = str(normalized.get("start_date") or normalized.get("start") or "").strip()
            end_date = str(normalized.get("end_date") or normalized.get("end") or "").strip()
            if start_date:
                normalized["start_date"] = start_date[:10]
            if end_date:
                normalized["end_date"] = end_date[:10]
            result = await _execute_bulk_backfill(job_id, normalized)
        else:
            raise ValueError(f"Unknown job_type: {job_type}")

        result["duration_sec"] = time.time() - start_time
        mark_job_status(job_id, "completed")

        # Publish completed event
        # Always publish completion events to ensure Brain gets notified
        # (OOF script may also publish its own with richer metrics, but duplicate events are acceptable)
        _publish_ml_event({
            "event": "ml.job",
            "status": "completed",
            "job_id": job_id,
            "job_type": job_type,
            "ok": result.get("ok", False),
            # Include original job params so Brain/diagnostics can reconstruct context
            "params": params,
            "metrics": result.get("metrics", {}),
            "duration_sec": result["duration_sec"],
            "artifacts": result.get("artifacts", []),
            "ts": datetime.now().isoformat()
        })

        logger.info("job_execution_complete", job_id=job_id, ok=result["ok"], duration_sec=result["duration_sec"])

    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        result["duration_sec"] = time.time() - start_time
        mark_job_status(job_id, "failed")

        # Publish failed event
        _publish_ml_event({
            "event": "ml.job",
            "status": "failed",
            "job_id": job_id,
            "job_type": job_type,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "duration_sec": result["duration_sec"],
            "ts": datetime.now().isoformat()
        })

        logger.error("job_execution_failed", job_id=job_id, error=str(e), exc_info=True)

    return result


async def _execute_oof(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute OOF job."""
    # Resolve desired features table up-front so it's always defined
    target_ft = params.get("features_table") or params.get("features_table_override")
    try:
        from libs.config import config as _cfg
        if not target_ft:
            target_ft = ((_cfg.get("policies") or {}).get("ml") or {}).get("features_table_override")
    except Exception:
        pass
    if not target_ft:
        target_ft = "trading.features_1m_active"
    # Keep override in params for downstream command building
    params["features_table_override"] = params.get("features_table_override") or target_ft

    if is_market_closed():
        logger.info("market_closed_skip_ml", job_id=job_id)
        return {
            "ok": False,
            "returncode": 0,
            "stdout": "",
            "stderr": "Market closed; skipping ML job",
            "metrics": {"error_code": "market_closed"},
            "artifacts": [],
        }
    symbols = params.get("symbols", [])
    window_days = params.get("window_days", 30)
    mode = params.get("mode", "light")  # "light" or "full"

    # === NEW: PreFlight Check ===
    # Ensure data dependencies are ready before executing expensive ML job
    try:
        if is_market_closed():
            logger.info("market_closed_skip_ml", job_id=job_id)
            return {
                "ok": False,
                "returncode": 0,
                "stdout": "",
                "stderr": "Market closed; skipping ML job",
                "metrics": {"error_code": "market_closed"},
                "artifacts": [],
            }
        from scripts.ai_trader.brain.preflight import preflight_check_all
        from libs.healer.data_healer_v2 import DataHealerV2
        from libs.health.snapshot import (
            SystemHealthSnapshot,
            BarHealthStatus,
            FeatureHealthStatus,
            LabelHealthStatus,
            LabelSymbolStatus,
            ConfigHealthStatus,
        )
        # Align features/labels to latest bars before training (best effort)
        try:
            from libs.utils.market_calendar import last_data_day
            from libs.db.ch_pool import get_client
            import subprocess

            data_day = last_data_day(symbols)
            if data_day:
                ch_local = get_client()
                feat_res = ch_local.query(
                    """
                    SELECT max(ts) AS max_ts FROM trading.features_1m
                    WHERE symbol IN %(syms)s
                    """,
                    {"syms": symbols or ["TSLA", "NVDA"]},
                )
                label_res = ch_local.query(
                    """
                    SELECT max(ts) AS max_ts FROM trading.features_1m_with_labels
                    WHERE symbol IN %(syms)s
                    """,
                    {"syms": symbols or ["TSLA", "NVDA"]},
                )
                feat_max = feat_res[0].get("max_ts") if feat_res else None
                label_max = label_res[0].get("max_ts") if label_res else None

                # Recompute features if stale
                if not feat_max or feat_max.date() < data_day:
                    start_dt = feat_max.date() if feat_max else (data_day - timedelta(days=1))
                    logger.info("auto_recompute_features", job_id=job_id, start=start_dt.isoformat(), end=data_day.isoformat(), symbols=symbols)
                    subprocess.run(
                        [
                            sys.executable,
                            "tool/compute_features.py",
                            "--bar-size",
                            "1m",
                            "--symbols",
                            *(symbols or ["TSLA", "NVDA"]),
                            "--start",
                            start_dt.isoformat(),
                            "--end",
                            data_day.isoformat(),
                        ],
                        check=False,
                        cwd=str(_PROJECT_ROOT),
                    )
                # Recompute labels if stale
                if not label_max or label_max.date() < data_day:
                    start_dt = label_max.date() if label_max else (data_day - timedelta(days=1))
                    start = f"{start_dt} 09:30:00"
                    end = f"{data_day} 16:00:00"
                    logger.info("auto_recompute_labels", job_id=job_id, start=start, end=end, symbols=symbols)
                    subprocess.run(
                        [
                            sys.executable,
                            "tools/recompute_labels_1m.py",
                            "--symbols",
                            ",".join(symbols or ["TSLA", "NVDA"]),
                            "--start",
                            start,
                            "--end",
                            end,
                            "--label-col",
                            f"label_h{int(params.get('horizon_min', 30))}",
                        ],
                        check=False,
                        cwd=str(_PROJECT_ROOT),
                    )
        except Exception as auto_exc:
            logger.warning("auto_feature_label_recompute_failed", job_id=job_id, error=str(auto_exc))

        # === DataReadinessChecker: Fast pre-filter before expensive preflight ===
        try:
            from libs.ml.data_readiness_checker import DataReadinessChecker

            horizon_param = params.get("horizon_min")
            if horizon_param is None:
                horizon_param = 30  # default to 30m to align with config/oof_policies.json
                params["horizon_min"] = horizon_param

            readiness_checker = DataReadinessChecker()
            end_dt = date.today()
            start_dt = end_dt - timedelta(days=window_days)
            start_iso = f"{start_dt} 09:30:00"
            end_iso = f"{end_dt} 16:00:00"

            symbols_list = symbols or ["TSLA", "NVDA"]
            rec = readiness_checker.check(
                symbols=symbols_list,
                start=start_iso,
                end=end_iso,
                features_table=target_ft,
                labels_table="trading.features_1m_with_labels",
            )
            logger.info(
                "data_readiness_summary",
                job_id=job_id,
                status=rec.status,
                reason=rec.reason,
                suggested_start=rec.suggested_start,
                suggested_end=rec.suggested_end,
                coverage=rec.coverage,
            )
            if rec.status == "needs_window_adjust" and rec.suggested_start and rec.suggested_end:
                try:
                    start_parsed = datetime.fromisoformat(rec.suggested_start.replace(" ", "T"))
                    end_parsed = datetime.fromisoformat(rec.suggested_end.replace(" ", "T"))
                    window_days = max(1, (end_parsed - start_parsed).days)
                    params["window_days"] = window_days
                    params["start_override"] = rec.suggested_start
                    params["end_override"] = rec.suggested_end
                    logger.info("window_days_adjusted", job_id=job_id, window_days=window_days)
                except Exception:
                    pass
            elif rec.status == "missing":
                # fail fast to avoid empty training
                return {
                    "ok": False,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "Data readiness failed: no bars found",
                    "metrics": {"error_code": "data_not_ready"},
                    "artifacts": [],
                }
        except Exception as exc:
            logger.warning("data_readiness_checker_failed", job_id=job_id, error=str(exc))

        # === ActiveTableManager: Auto-sync or fallback if active table is stale ===
        try:
            from libs.ml.active_table_manager import ActiveTableManager

            # Only check/sync if using active table
            if target_ft == "trading.features_1m_active":
                manager = ActiveTableManager()
                sync_result = manager.auto_sync_or_fallback(symbols=symbols_list)

                # Update target_ft based on sync result
                target_ft = sync_result.features_table
                params["features_table"] = target_ft

                logger.info(
                    "active_table_check_complete",
                    job_id=job_id,
                    action=sync_result.action,
                    success=sync_result.success,
                    features_table=sync_result.features_table,
                    rows_synced=sync_result.rows_synced,
                    error=sync_result.error,
                )

                # If sync/fallback failed completely, abort training
                if not sync_result.success and sync_result.action == "fallback":
                    return {
                        "ok": False,
                        "returncode": 0,
                        "stdout": "",
                        "stderr": f"Active table sync failed and fallback failed: {sync_result.error}",
                        "metrics": {"error_code": "active_table_unavailable"},
                        "artifacts": [],
                    }
        except Exception as exc:
            logger.warning("active_table_manager_failed", job_id=job_id, error=str(exc))
            # Fall back to baseline on any error
            target_ft = "trading.features_1m"
            params["features_table"] = target_ft

        logger.info("preflight_check_starting", job_id=job_id, symbols=symbols)

        # === Bad Symbols Check: Block training on symbols with data quality issues ===
        try:
            from datetime import date as dt_date
            from pathlib import Path
            import json

            # Check today's and yesterday's bad symbols reports
            today_dir = Path("runs/data_quality") / dt_date.today().strftime("%Y%m%d")
            yesterday_dir = Path("runs/data_quality") / (dt_date.today() - timedelta(days=1)).strftime("%Y%m%d")

            bad_symbols_set = set()
            for check_dir in [today_dir, yesterday_dir]:
                bad_symbols_file = check_dir / "bad_symbols.json"
                if bad_symbols_file.exists():
                    try:
                        bad_data = json.loads(bad_symbols_file.read_text())
                        bad_symbols_set.update(bad_data.get("bad_symbols", []))
                    except Exception:
                        pass

            if bad_symbols_set:
                # Filter out bad symbols from training
                symbols_list = symbols or ["TSLA", "NVDA"]
                blocked_symbols = [s for s in symbols_list if s in bad_symbols_set]

                if blocked_symbols:
                    logger.warning(
                        "bad_symbols_blocked",
                        job_id=job_id,
                        blocked=blocked_symbols,
                        total_requested=len(symbols_list)
                    )

                    # If all symbols are bad, fail the job
                    if len(blocked_symbols) == len(symbols_list):
                        return {
                            "ok": False,
                            "returncode": 0,
                            "stdout": "",
                            "stderr": f"All symbols blocked due to data quality issues: {blocked_symbols}",
                            "metrics": {"error_code": "all_symbols_bad_quality"},
                            "artifacts": [],
                        }

                    # Otherwise, filter out bad symbols and continue
                    symbols = [s for s in symbols_list if s not in bad_symbols_set]
                    params["symbols"] = symbols
                    logger.info("bad_symbols_filtered", job_id=job_id, remaining_symbols=symbols)
        except Exception as exc:
            logger.warning("bad_symbols_check_failed", job_id=job_id, error=str(exc))

        # Skip ultra-short horizons during holiday/weekend to avoid noisy/zero labels
        def _is_holiday_or_weekend():
            # Reuse central market calendar to avoid manual toggles/HOLIDAY_MODE drift
            return is_market_closed()

        if int(horizon_param) <= 5 and _is_holiday_or_weekend():
            logger.warning(
                "skip_short_horizon_holiday",
                job_id=job_id,
                horizon_min=horizon_param
            )
            return {
                "ok": False,
                "returncode": 0,
                "stdout": "",
                "stderr": "Skipped h5 during holiday/weekend",
                "artifacts": [],
                "metrics": {
                    "error_code": "skip_short_holiday",
                    "horizon_min": horizon_param
                }
            }

        # Run preflight checks for the target horizon (respect overrides if present)
        start_override = params.get("start_override")
        end_override = params.get("end_override")
        ready, preflight_report = preflight_check_all(
            symbols=symbols if symbols else ["AAPL", "TSLA", "NVDA"],
            horizons=[int(horizon_param)],
            min_rows_per_symbol=1000,
            strict=False,  # Only labels are blocking
            lookback_days=window_days,  # Use training window for consistency
            features_table=target_ft,
            labels_table="trading.features_1m_with_labels",
            start=start_override or start_param,
            end=end_override or end_param,
            data_quality_gate=True,
            data_quality_days=min(window_days, 3),
        )

        if not ready:
            blocking_issues = preflight_report.get('blocking_issues', [])

            # Surface NVDA-specific problems early so operators can address them quickly
            nvda_issues = [
                issue for issue in blocking_issues
                if any(str(sym).upper() == "NVDA" for sym in issue.get('symbols', []))
            ]
            if nvda_issues:
                logger.warning(
                    "preflight_nvda_issue_detected",
                    job_id=job_id,
                    issues=nvda_issues
                )

            logger.warning(
                "preflight_check_failed",
                job_id=job_id,
                blocking_issues=blocking_issues
            )

            # Attempt auto-healing only when labels are truly not ready
            healer = DataHealerV2(runs_dir="runs/ai_trader")

            # Evaluate readiness flags
            labels_info = preflight_report.get("labels", {}) if isinstance(preflight_report.get("labels"), dict) else {}
            labels_ready = labels_info.get("overall_ready")
            if labels_ready is None:
                status = labels_info.get("status")
                labels_ready = status == "ready" if status else preflight_report.get("ready", False)

            features_info = preflight_report.get("features", {}) if isinstance(preflight_report.get("features"), dict) else {}
            features_ready = features_info.get("status") == "ready"

            # Build snapshot for DataHealerV2
            def _snapshot_from_preflight(report: Dict[str, Any]) -> SystemHealthSnapshot:
                sym_list = symbols if symbols else ["AAPL", "TSLA", "NVDA"]
                horizons = [int(horizon_param)]
                bars = BarHealthStatus(status="healthy")
                feat_status_str = "blocked" if not features_ready else "healthy"
                feat_age = 0.0
                by_sym_features = features_info.get("by_symbol") or {}
                for _, fs in by_sym_features.items():
                    try:
                        a = float(fs.get("age_hours", 0.0) or 0.0)
                        if a > feat_age:
                            feat_age = a
                    except Exception:
                        continue
                feat = FeatureHealthStatus(status=feat_status_str, age_hours=feat_age)
                lbls = LabelHealthStatus(status="healthy", by_symbol={}, summary=labels_info.get("summary", {}))
                by_symbol = labels_info.get("by_symbol") or {}
                for sym, s in by_symbol.items():
                    lbls.by_symbol[sym] = LabelSymbolStatus(
                        symbol=sym,
                        horizon_min=int(horizon_param),
                        rows=int(s.get("rows", 0) or 0),
                        null_ratio=float(s.get("null_ratio", 0.0) or 0.0),
                        age_hours=float(s.get("age_hours", 0.0) or 0.0) if s.get("age_hours") is not None else 0.0,
                        status="blocked" if s.get("status") in {"missing", "high_nulls", "stale"} else "healthy",
                        issues=s.get("issues", []),
                    )
                    if lbls.by_symbol[sym].status == "blocked":
                        lbls.status = "blocked"
                cfg = ConfigHealthStatus(status="healthy", problems=[])
                overall_status = "healthy" if ready else "blocked"
                return SystemHealthSnapshot(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    symbols=sym_list,
                    horizons=horizons,
                    bars=bars,
                    features=feat,
                    labels=lbls,
                    config=cfg,
                    overall_status=overall_status,
                    blocking_issues=preflight_report.get("blocking_issues", []),
                )

            snapshot = _snapshot_from_preflight(preflight_report)

            if not labels_ready or not features_ready:
                logger.info("attempting_auto_heal_v2", job_id=job_id)
                heal_results = healer.heal(snapshot)
                # Normalize to list
                if isinstance(heal_results, list):
                    results_list = heal_results
                else:
                    results_list = [heal_results]
                success_count = sum(1 for r in results_list if getattr(r, "success", False))
                if success_count == 0:
                    logger.error(
                        "label_auto_heal_failed",
                        job_id=job_id,
                        errors=[getattr(r, "error", "unknown") for r in results_list],
                    )
                    return {
                        "ok": False,
                        "returncode": -1,
                        "stdout": "",
                        "stderr": "PreFlight check failed: heal unsuccessful",
                        "artifacts": [],
                        "metrics": {
                            "error_code": "preflight_label_heal_failed",
                            "preflight_report": preflight_report,
                            "heal_result": [getattr(r, 'error', None) for r in results_list],
                            "nvda_issues": nvda_issues
                        }
                    }
                else:
                    logger.info(
                        "label_auto_heal_success",
                        job_id=job_id,
                        actions=len(results_list),
                        success_count=success_count
                    )
            else:
                # Labels are OK but features or other non-blocking items might be missing; continue in non-strict mode
                logger.warning(
                    "preflight_warning_non_blocking",
                    job_id=job_id,
                    issues=blocking_issues
                )
        else:
            logger.info("preflight_check_passed", job_id=job_id)

    except Exception as e:
        # Don't fail the job on preflight errors - log and continue
        logger.error(
            "preflight_check_exception",
            job_id=job_id,
            error=str(e),
            exc_info=True
        )
        # Continue with job execution despite preflight error

    # Build command
    horizon_param = params.get("horizon_min")
    if horizon_param is None:
        horizon_param = 30  # default to 30m to align with config/oof_policies.json
        params["horizon_min"] = horizon_param

    # General coverage guard: try to regenerate labels for the requested horizon; if still too low, optionally bump to 30m, otherwise abort to avoid n_train=0 loops.
    try:
        def _coverage(label_col: str, days: int) -> int:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            q = f"""
            SELECT countIf({label_col} IS NOT NULL) AS cnt
            FROM trading.features_1m_with_labels
            WHERE symbol IN %(syms)s AND toDate(ts) >= %(cutoff)s
            """
            res = ch.query(q, {"syms": symbols or ["TSLA", "NVDA"], "cutoff": cutoff})
            return int(res[0].get("cnt", 0) if res else 0)

        def _null_ratio(label_col: str, days: int) -> float:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            q = f"""
            SELECT count() AS total, countIf({label_col} IS NULL) AS nulls
            FROM trading.features_1m_with_labels
            WHERE symbol IN %(syms)s AND toDate(ts) >= %(cutoff)s
            """
            res = ch.query(q, {"syms": symbols or ["TSLA", "NVDA"], "cutoff": cutoff})
            if not res or res[0].get("total", 0) == 0:
                return 1.0
            total = res[0].get("total", 0) or 0
            nulls = res[0].get("nulls", 0) or 0
            return float(nulls) / float(total) if total else 1.0

        ch = ClickHouseDB()
        label_col = f"label_h{int(horizon_param)}"
        coverage_days = max(7, min(window_days, 30))
        min_cov = int(os.getenv("MIN_LABEL_COVERAGE", 100))
        max_null_ratio = float(os.getenv("MAX_NULL_RATIO", "0.1") or 0.1)
        relax_max_null_ratio_env = os.getenv("RELAX_MAX_NULL_RATIO")
        relax_max_null_ratio = float(relax_max_null_ratio_env) if relax_max_null_ratio_env else None

        cov = _coverage(label_col, coverage_days)
        null_ratio = _null_ratio(label_col, coverage_days)

        # If null ratio is high but within relaxed range, prefer a shorter horizon (e.g., 15 -> 5) once.
        if (
            relax_max_null_ratio is not None
            and null_ratio > max_null_ratio
            and null_ratio <= relax_max_null_ratio
            and int(horizon_param) > 5
        ):
            new_h = 15 if int(horizon_param) > 15 else 5
            logger.warning(
                "oof_bump_horizon_due_to_high_nulls",
                job_id=job_id,
                old_horizon=horizon_param,
                new_horizon=new_h,
                null_ratio=round(null_ratio, 4),
                max_null_ratio=max_null_ratio,
                relax_max_null_ratio=relax_max_null_ratio,
            )
            horizon_param = new_h
            params["horizon_min"] = horizon_param
            label_col = f"label_h{int(horizon_param)}"
            # Recompute coverage/nulls for the new horizon
            cov = _coverage(label_col, coverage_days)
            null_ratio = _null_ratio(label_col, coverage_days)

        if cov < min_cov:
            # attempt targeted regeneration
            try:
                logger.warning("oof_regen_label_due_to_low_coverage", job_id=job_id, symbols=symbols, horizon=horizon_param, coverage=cov)
                regen_cmd = [
                    sys.executable,
                    "tools/recompute_labels_1m.py",
                    "--symbols", ",".join(symbols) if symbols else "TSLA,NVDA",
                    "--start", (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d 09:30:00"),
                    "--end", datetime.now().strftime("%Y-%m-%d 16:00:00"),
                    "--horizons", str(int(horizon_param)),
                ]
                subprocess.run(regen_cmd, check=False, text=True, capture_output=True, cwd=str(_PROJECT_ROOT))
                cov = _coverage(label_col, coverage_days)
                null_ratio = _null_ratio(label_col, coverage_days)
            except Exception:
                cov = 0

            if cov < min_cov:
                # Try bump to 30m if different and coverage exists there
                if int(horizon_param) != 30:
                    cov30 = _coverage("label_h30", coverage_days)
                    if cov30 >= min_cov:
                        logger.warning("oof_bump_horizon_due_to_low_coverage", job_id=job_id, old_horizon=horizon_param, new_horizon=30)
                        horizon_param = 30
                        params["horizon_min"] = horizon_param
                    else:
                        logger.error("oof_label_coverage_insufficient", job_id=job_id, horizon=horizon_param, coverage=cov, coverage_30m=cov30)
                        return {
                            "ok": False,
                            "returncode": 0,
                            "stdout": "",
                            "stderr": "Label coverage insufficient",
                            "metrics": {"error_code": "label_coverage_insufficient", "horizon_min": horizon_param, "coverage": cov},
                            "artifacts": [],
                        }
                else:
                    logger.error("oof_label_coverage_insufficient", job_id=job_id, horizon=horizon_param, coverage=cov)
                    return {
                        "ok": False,
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "Label coverage insufficient",
                        "metrics": {"error_code": "label_coverage_insufficient", "horizon_min": horizon_param, "coverage": cov},
                        "artifacts": [],
                    }
    except Exception:
        pass

    # Build nightly_oof_flow command with readiness-adjusted window if present
    cmd = [
        sys.executable,
        "tools/nightly_oof_flow.py",
        "--symbols", ",".join(symbols) if symbols else "AAPL,TSLA,NVDA",
    ]
    # If we decided on a feature table override, pass it to nightly_oof_flow so training uses the right data
    if params.get("features_table_override"):
        cmd.extend(["--features-table-override", str(params["features_table_override"])])

    # Derive start/end if not explicitly provided, allow overrides from readiness
    start_param = params.get("start_override") or params.get("start")
    end_param = params.get("end_override") or params.get("end")
    clamp_to_last_trading_day = os.getenv("CLAMP_TO_LAST_TRADING_DAY", "1").lower() in ("1", "true", "yes", "on")
    abort_if_no_bars = os.getenv("ABORT_IF_NO_BARS", "1").lower() in ("1", "true", "yes", "on")
    # Determine features table health and auto-switch to baseline if target is empty/stale
    def _feature_table_health(table_name: str) -> tuple[date | None, int]:
        try:
            from libs.db.ch_pool import get_client
            ch_ft = get_client()
            res = ch_ft.query(
                f"""
                SELECT max(ts) AS max_ts, count() AS rows
                FROM {table_name}
                """
            )
            if res:
                max_ts = res[0].get("max_ts")
                rows = int(res[0].get("rows", 0) or 0)
                return (max_ts.date() if max_ts else None, rows)
        except Exception:
            return (None, 0)
        return (None, 0)

    def _should_sync_active_from_health(symbols: list[str]) -> bool:
        """
        Check mutable_targets snapshot; if active is recommended sync_or_fallback, trigger sync.
        """
        try:
            mt_fp = Path("runs/memory/mutable_targets.json")
            if not mt_fp.exists():
                return False
            data = json.loads(mt_fp.read_text())
            active = data.get("data:features_1m_active") or {}
            rec = (active.get("metrics") or {}).get("recommendation")
            if rec == "sync_or_fallback":
                return True
        except Exception:
            return False
        return False

    ft_max_date, ft_rows = _feature_table_health(target_ft)
    min_ft_rows = int(os.getenv("MIN_FEATURE_ROWS", 1000))
    bars_day_probe = None  # populate below

    def _last_data_day_from_bars() -> date | None:
        try:
            from libs.db.ch_pool import get_client
            ch_local = get_client()
            res = ch_local.query(
                """
                SELECT max(ts_end) AS max_ts
                FROM trading.bars_1m_ibkr
                WHERE symbol IN %(syms)s
                """,
                {"syms": symbols or ["TSLA", "NVDA"]},
            )
            if res and res[0].get("max_ts"):
                try:
                    return res[0]["max_ts"].date()
                except Exception:
                    pass
        except Exception:
            pass
        return None

    clamp_metadata: Dict[str, Any] = {}

    # Early abort if there are no bars at all (max(ts_end) is null)
    if abort_if_no_bars:
        bars_day_probe = _last_data_day_from_bars()
        if bars_day_probe is None:
            logger.warning("oof_aborted_no_bars_found", job_id=job_id)
            return {
                "ok": False,
                "returncode": 0,
                "stdout": "",
                "stderr": "No bars available; aborting OOF job",
                "metrics": {"error_code": "no_bars_found"},
                "artifacts": [],
            }
        # If features table is empty/stale/too small vs bars, or health snapshot recommends sync, try chunked sync; else switch to baseline
        force_sync = _should_sync_active_from_health(symbols)
        if ft_rows < min_ft_rows or (ft_max_date and ft_max_date < bars_day_probe) or force_sync:
            try:
                if target_ft == "trading.features_1m_active":
                    logger.warning("features_active_sync_start", job_id=job_id)
                    try:
                        publish_ml_event(
                            "started",
                            "features_active_sync",
                            job_id=f"sync-{job_id}",
                            params={
                                "symbols": symbols,
                                "sync_days": os.getenv("ACTIVE_SYNC_DAYS", 7),
                                "reason": "force_sync" if force_sync else "stale_or_empty",
                            },
                        )
                    except Exception:
                        pass
                    sync_days = int(os.getenv("ACTIVE_SYNC_DAYS", 7))
                    start_sync = bars_day_probe - timedelta(days=sync_days)
                    subprocess.run(
                        [
                            sys.executable,
                            "tools/sync_features_active.py",
                            "--symbols",
                            ",".join(symbols or ["TSLA", "NVDA"]),
                            "--start",
                            f"{start_sync} 09:30:00",
                            "--end",
                            f"{bars_day_probe} 16:00:00",
                            "--chunk-days",
                            str(os.getenv("ACTIVE_SYNC_CHUNK_DAYS", 1)),
                        ],
                        check=False,
                        cwd=str(_PROJECT_ROOT),
                    )
                    ft_max_date, ft_rows = _feature_table_health(target_ft)
                    logger.warning("features_active_sync_end", job_id=job_id, ft_rows=ft_rows, ft_max_date=str(ft_max_date))
                    try:
                        publish_ml_event(
                            "completed",
                            "features_active_sync",
                            job_id=f"sync-{job_id}",
                            ok=True,
                            metrics={"ft_rows": ft_rows},
                        )
                    except Exception:
                        pass
            except Exception as sync_exc:
                logger.warning("features_active_sync_failed", job_id=job_id, error=str(sync_exc))
                try:
                    publish_ml_event(
                        "completed",
                        "features_active_sync",
                        job_id=f"sync-{job_id}",
                        ok=False,
                        metrics={"error": str(sync_exc)},
                    )
                except Exception:
                    pass

        if ft_rows == 0 or (ft_max_date and ft_max_date < bars_day_probe):
            params["features_table_override"] = "trading.features_1m"
            target_ft = params["features_table_override"]
            logger.warning(
                "features_table_switched_to_baseline",
                job_id=job_id,
                target_ft=target_ft,
                original_ft=params.get("features_table") or params.get("features_table_override") or "trading.features_1m_active",
                ft_rows=ft_rows,
                ft_max_date=str(ft_max_date) if ft_max_date else None,
                bars_max=bars_day_probe.isoformat(),
            )
            ft_max_date, ft_rows = _feature_table_health(target_ft)
            if ft_rows == 0:
                logger.warning("oof_aborted_no_features_found", job_id=job_id, target_ft=target_ft)
                return {
                    "ok": False,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "No features available; aborting OOF job",
                    "metrics": {"error_code": "no_features_found", "features_table": target_ft},
                    "artifacts": [],
                }

    if start_param and end_param:
        # Even if caller provided dates, optionally clamp end to last data day to avoid empty/holiday tails.
        try:
            from datetime import datetime as _dt
            end_dt = _dt.fromisoformat(str(end_param))
            if clamp_to_last_trading_day:
                data_day = _last_data_day_from_bars()
                if data_day and data_day < end_dt.date():
                    end_dt = _dt.combine(data_day, _dt.strptime("16:00:00", "%H:%M:%S").time())
                    clamp_metadata["clamped_to_last_data_day"] = data_day.isoformat()
                    logger.info(
                        "oof_end_clamped_to_last_data_day",
                        job_id=job_id,
                        last_data_day=data_day.isoformat(),
                        original_end=str(end_param),
                    )
            end_param = end_dt.strftime("%Y-%m-%d %H:%M:%S")
            # Adjust start if it was after the clamped end; otherwise keep user-provided.
            try:
                start_dt_parsed = _dt.fromisoformat(str(start_param))
                if start_dt_parsed > end_dt:
                    start_dt_parsed = end_dt - timedelta(days=int(window_days))
                    clamp_metadata["start_shifted_with_clamp"] = True
                    logger.info(
                        "oof_start_shifted_due_to_end_clamp",
                        job_id=job_id,
                        start=start_dt_parsed.strftime("%Y-%m-%d %H:%M:%S"),
                        end=end_param,
                        window_days=window_days,
                    )
                start_param = start_dt_parsed.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                start_param = str(start_param)
        except Exception:
            pass
        cmd.extend(["--start", str(start_param), "--end", str(end_param)])
    else:
        # Default: last N calendar days, RTH bounds, clamped to last data day if available
        try:
            from datetime import datetime as _dt, timedelta as _td
            end_dt = _dt.combine(date.today(), _dt.strptime("16:00:00", "%H:%M:%S").time())
            if clamp_to_last_trading_day:
                data_day = _last_data_day_from_bars()
                if data_day and data_day < end_dt.date():
                    end_dt = _dt.combine(data_day, _dt.strptime("16:00:00", "%H:%M:%S").time())
                    clamp_metadata["clamped_to_last_data_day"] = data_day.isoformat()
                    logger.info(
                        "oof_end_clamped_to_last_data_day",
                        job_id=job_id,
                        last_data_day=data_day.isoformat(),
                        original_end=params.get("end_override") or params.get("end") or "today",
                    )
            start_dt = end_dt - _td(days=int(window_days))
            # prefer readiness overrides if present
            if params.get("start_override"):
                start_dt = _dt.fromisoformat(str(params["start_override"]).replace("T", " "))
            if params.get("end_override"):
                end_dt = _dt.fromisoformat(str(params["end_override"]).replace("T", " "))
            cmd.extend(
                [
                    "--start",
                    start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "--end",
                    end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ]
            )
        except Exception:
            # Fallback to simple ISO dates if time combine fails
            cmd.extend(["--start", (date.today() - timedelta(days=int(window_days))).isoformat(),
                        "--end", date.today().isoformat()])

    # Optional training window hint for policy
    cmd.extend(["--window-days", str(window_days)])

    if mode == "light":
        cmd.extend(["--top-k", "10", "--quick"])

    # If configured, override features table to active (RFE sink)
    try:
        from libs.config import config as _cfg
        use_active = bool(((_cfg.get('policies') or {}).get('features') or {}).get('use_rfe_active_table', False))
    except Exception:
        use_active = False
    if use_active:
        cmd.extend(["--features-table-override", "trading.features_1m_active"])

    logger.info("executing_oof", job_id=job_id, command=" ".join(cmd))

    # Execute
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy()
    )

    stdout, stderr = await proc.communicate()

    rc = proc.returncode
    stdout_str = stdout.decode("utf-8", errors="ignore") if stdout else ""
    stderr_str = stderr.decode("utf-8", errors="ignore") if stderr else ""

    metrics: Dict[str, Any] = clamp_metadata
    # Always record which features table we attempted to use for auditing
    metrics["features_table"] = target_ft
    eval_dir = _runs_dir() / "oof_eval"
    artifacts: List[str] = [str(eval_dir)] if eval_dir.exists() else []
    if rc != 0:
        # Provide a coarse error_code so Brain/diagnostics can distinguish data issues
        err = stderr_str or stdout_str
        err_lower = err.lower()
        if "the following arguments are required: --start, --end" in err_lower:
            error_code = "oof_cli_missing_start_end"
        elif "jsondecodeerror" in err_lower or "expecting value: line 1 column 1" in err_lower:
            # Invalid or empty JSON in OOF eval artifacts
            error_code = "oof_json_invalid"
        elif "unknown table" in err_lower:
            error_code = "oof_clickhouse_unknown_table"
        elif "unknown function" in err_lower:
            error_code = "oof_clickhouse_unknown_function"
        elif "connection refused" in err_lower:
            error_code = "oof_clickhouse_connection_refused"
        else:
            error_code = "oof_cli_failed"
        metrics = {
            "error_code": error_code,
            # Short tail for debugging; safe size for ML events
            "stderr_tail": stderr_str[-300:] if stderr_str else "",
        }
        metrics["features_table"] = target_ft
        logger.error("oof_cli_failed",
                     job_id=job_id,
                     returncode=rc,
                     error_code=error_code,
                     stderr_tail=metrics["stderr_tail"])
    else:
        # Best-effort parse eval outputs to surface ev/beta_corr/coverage to ml_events
        agg_file = eval_dir / "oof_eval.json"
        if agg_file.exists():
            try:
                agg = json.loads(agg_file.read_text())
                fusion = agg.get("fusion") or {}
                params = agg.get("params") or {}
                n_bars = float(params.get("n_bars", 0) or 0)
                n_valid = float(fusion.get("n", 0) or 0)
                coverage = (n_valid / n_bars) if n_bars > 0 else None
                metrics.update({
                    "ev": fusion.get("ev", agg.get("ev")),
                    "beta_corr": fusion.get("beta_corr") or fusion.get("corr_to_spy") or agg.get("beta_corr"),
                    "auc": fusion.get("auc"),
                    "ic": fusion.get("ic"),
                    "coverage": coverage,
                    "n_bars": n_bars or None,
                    "horizon_min": params.get("horizon_min"),
                })
            except Exception as parse_exc:
                logger.warning("oof_eval_parse_failed", job_id=job_id, error=str(parse_exc))

    # Ensure keys exist so downstream gates are never missing ev/beta_corr
    metrics.setdefault("ev", None)
    metrics.setdefault("beta_corr", None)

    return {
        "ok": rc == 0,
        "returncode": rc,
        "stdout": stdout_str[-1000:],
        "stderr": stderr_str[-1000:],
        "artifacts": artifacts,
        "metrics": metrics,
    }


async def _execute_alpha_loop(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute Alpha Loop HPO job.

    Args:
        job_id: Job ID
        params: Job parameters with:
            - symbols: Optional list of symbols to optimize (default: all enabled)
            - days: Number of days for HPO window (default: 30)
            - samples: Number of samples (default: 100)
            - timeout_hours: Job timeout in hours (default: 1, max: 2)

    Returns:
        Execution result with metrics and artifacts
    """
    result = {
        "ok": False,
        "error": None,
        "artifacts": [],
        "metrics": {}
    }

    try:
        # Extract nested params if present (Brain uses params.params structure)
        # Fallback to top-level params for backward compatibility
        job_params = params.get("params", params) if isinstance(params.get("params"), dict) else params

        # Extract and validate params
        symbols = job_params.get("symbols", [])
        if not symbols:
            # Fallback to configured universe to avoid strategy extras error
            try:
                from libs.config import config as _cfg
                symbols = [str(s) for s in (_cfg.universe or [])]
            except Exception:
                symbols = []
        if not symbols:
            # Final safety: default to TSLA to prevent misconfiguration crash; log clearly
            logger.warning("alpha_loop_missing_symbols_fallback", job_id=job_id, fallback_symbol="TSLA")
            symbols = ["TSLA"]
        days = int(job_params.get("days", 30))
        # Accept either 'samples' or 'n_trials' as Brain-provided controls
        samples = int(job_params.get("samples", job_params.get("n_trials", 100)))

        timeout_hours = float(job_params.get("timeout_hours", 1.0))
        timeout_hours = min(max(timeout_hours, 0.1), 2.0)  # Clamp to [0.1, 2.0]

        out_dir = _runs_dir() / f"hpo_{job_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info("alpha_loop_starting",
                   job_id=job_id,
                   symbols=symbols,
                   days=days,
                   samples=samples,
                   timeout_hours=timeout_hours,
                   raw_params=params)

        # Build command using nightly_hpo.py actual arguments
        timeout_sec = int(timeout_hours * 3600)
        cmd = [
            sys.executable,
            "tools/nightly_hpo.py",
            "--days", str(days),
            "--samples", str(samples),
            "--out-dir", str(out_dir),
            "--real-backtest",  # Enable real backtesting instead of mock
            "--wf",             # Use walk-forward validation
            "--is-days", "7",   # In-sample 7 days
            "--oos-days", "3",  # Out-of-sample 3 days
            "--step-days", "5"  # Step forward 5 days per fold
        ]

        if symbols:
            cmd.extend(["--symbols"] + symbols)

        # Optional: features table override from params or config
        try:
            ft = params.get("features_table") or ((_cfg.get("policies") or {}).get("ml") or {}).get("features_table_override")
            if ft:
                cmd += ["--features-table-override", str(ft)]
        except Exception:
            pass

        # Execute with timeout
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_sec
            )
            returncode = proc.returncode

            stdout_str = stdout.decode("utf-8") if stdout else ""
            stderr_str = stderr.decode("utf-8") if stderr else ""

            if returncode == 0:
                result["ok"] = True
                result["artifacts"] = [str(out_dir)]

                # Parse the overall JSON output from nightly_hpo.py
                # It now includes aggregated hpo_metrics at the top level
                try:
                    full_output_json = json.loads(stdout_str)
                    hpo_metrics = full_output_json.get("hpo_metrics", {})
                    # Also include other relevant top-level info from nightly_hpo.py
                    hpo_metrics["symbols"] = full_output_json.get("symbols", symbols)
                    hpo_metrics["days"] = days
                    hpo_metrics["samples"] = samples

                    if hpo_metrics:
                        result["metrics"] = hpo_metrics
                        logger.info("alpha_loop_completed", job_id=job_id, returncode=returncode,
                                   metrics=hpo_metrics, stdout_preview=stdout_str[-200:])
                    else:
                        # Fallback if hpo_metrics are unexpectedly empty
                        logger.warning("alpha_loop_metrics_empty", job_id=job_id, stdout_preview=stdout_str[-200:])
                        result["metrics"] = {
                            "error_code": "hpo_metrics_empty",
                            "stdout_tail": stdout_str[-200:]
                        }
                        result["ok"] = False # Mark as failed if no metrics
                except json.JSONDecodeError as e:
                    result["error"] = f"Failed to parse HPO script output: {e}"
                    result["ok"] = False
                    logger.error("alpha_loop_json_parse_failed", job_id=job_id, error=str(e), stdout_tail=stdout_str[-500:])

            else:
                result["error"] = f"HPO failed with returncode {returncode}"
                logger.error("alpha_loop_failed",
                            job_id=job_id,
                            returncode=returncode,
                            stderr=stderr_str[-500:])

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            result["error"] = f"HPO timed out after {timeout_hours}h"
            logger.error("alpha_loop_timeout", job_id=job_id, timeout_hours=timeout_hours)

    except Exception as e:
        result["error"] = str(e)
        logger.error("alpha_loop_execution_error", job_id=job_id, error=str(e), exc_info=True)

    return result


async def _execute_strategy_experiment(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a strategy experiment through validate -> run -> summarize -> robustness -> persist."""
    job_params = params.get("params", params) if isinstance(params.get("params"), dict) else params
    spec = _normalize_strategy_experiment_spec(job_id, job_params)
    experiment_id = str(spec.get("experiment_id") or job_id)

    logger.info(
        "strategy_experiment_starting",
        job_id=job_id,
        experiment_id=experiment_id,
        market=spec.get("market"),
        strategy_family=spec.get("strategy_family"),
        adapter=spec.get("engine", {}).get("adapter"),
    )

    try:
        experiment_api.create_experiment({
            "experiment_id": experiment_id,
            "kind": "strategy_experiment",
            "job_id": job_id,
            "spec": _json_safe(spec),
        })
    except Exception:
        pass

    validation = _validate_strategy_experiment_spec(spec)
    run_result: Dict[str, Any] = {
        "ok": False,
        "status": "BLOCKED",
        "error": None,
        "artifacts": [],
        "metrics": {},
    }
    robustness_result: Dict[str, Any] = {
        "ok": True,
        "status": "SKIPPED",
        "skipped": True,
        "metrics": {},
        "artifacts": [],
    }
    final_result: Dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "artifacts": [],
        "metrics": {},
        "error": None,
    }

    try:
        if validation["ok"]:
            run_result = _run_strategy_experiment_adapter(spec)
            if bool(run_result.get("ok", False)):
                robustness_result = _run_strategy_experiment_robustness_hook(spec, run_result)
            else:
                robustness_result = {
                    "ok": False,
                    "status": "FAILED",
                    "skipped": True,
                    "reason": run_result.get("error") or "strategy_runner_failed",
                    "wf_pass": False,
                    "embargo_pass": False,
                    "leakage_score": 1.0,
                    "regime_stability": 0.0,
                    "symbol_transfer_score": 0.0,
                    "metrics": {},
                    "artifacts": [],
                }
        else:
            run_result = {
                "ok": False,
                "status": "BLOCKED",
                "error": "validation_failed",
                "artifacts": [],
                "metrics": {},
            }
            robustness_result = {
                "ok": False,
                "status": "BLOCKED",
                "skipped": True,
                "reason": "validation_failed",
                "issues": validation.get("issues") or [],
                "wf_pass": False,
                "embargo_pass": False,
                "leakage_score": 1.0,
                "regime_stability": 0.0,
                "symbol_transfer_score": 0.0,
                "metrics": {},
                "artifacts": [],
            }

        final_result = _build_strategy_experiment_result(
            spec=spec,
            validation=validation,
            run_result=run_result,
            robustness_result=robustness_result,
            job_id=job_id,
        )
    except Exception as exc:
        logger.error("strategy_experiment_execution_error", job_id=job_id, error=str(exc), exc_info=True)
        failure_record = {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "artifacts": [],
            "metrics": {},
        }
        final_result = _build_strategy_experiment_result(
            spec=spec,
            validation=validation if validation else {"ok": False, "issues": ["unexpected_error"], "spec": spec},
            run_result=failure_record,
            robustness_result=robustness_result,
            job_id=job_id,
        )
        final_result["error"] = str(exc)

    _persist_strategy_experiment_tracking(
        job_id=job_id,
        spec=spec,
        validation=validation,
        run_result=run_result,
        robustness_result=robustness_result,
        final_result=final_result,
    )

    return final_result


async def _execute_lstm(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute LSTM training job."""
    # TODO: Implement LSTM execution
    logger.warning("lstm_execution_not_implemented", job_id=job_id)
    return {
        "ok": False,
        "error": "LSTM execution not implemented",
        "artifacts": [],
        "metrics": {}
    }


async def _execute_hpo_cli(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute upgraded HPO CLI (hpo_cli_upgraded.py) as a standalone job.

    Params (all optional; provide sensible defaults):
      - config: path to HPO JSON config
      - runner: default 'wf_hpo:evaluate_trial'
      - backend: default 'optuna-tpe'
      - study_name: default f'HPO-{job_id}'
      - storage: e.g. 'sqlite:///study.db' (will be created in outdir if relative)
      - n_trials: int, default 20
      - n_jobs: int, default 1
      - pruner: 'asha' | 'median' | None, default 'asha'
      - seed: int, default 2025

    Artifacts:
      - best_params JSON file (if created by CLI)
      - storage DB (if sqlite and created)
    """
    result = {"ok": False, "artifacts": [], "metrics": {}}
    try:
        from datetime import date
        out_dir = _runs_dir() / f"hpo_cli_{job_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Use absolute paths (project root) for all file references
        project_root = Path(__file__).resolve().parent.parent.parent

        config_path = params.get("config") or str(project_root / "config" / "hpo_tsla_working.json")
        # Convert relative paths to absolute
        if not Path(config_path).is_absolute():
            config_path = str(project_root / config_path)

        runner = params.get("runner") or "wf_hpo:evaluate_trial"
        backend = params.get("backend") or "optuna-tpe"
        study_name = params.get("study_name") or f"HPO-{job_id}"
        storage = params.get("storage") or f"sqlite:///{study_name}.db"
        n_trials = int(params.get("n_trials", 20))
        n_jobs = int(params.get("n_jobs", 1))
        pruner = params.get("pruner", "asha")
        seed = int(params.get("seed", 2025))

        hpo_cli_path = project_root / "hpo_cli_upgraded.py"

        cmd = [
            sys.executable,
            str(hpo_cli_path),
            "--config", str(config_path),
            "--runner", str(runner),
            "--backend", str(backend),
            "--study-name", str(study_name),
            "--storage", str(storage),
            "--n-trials", str(n_trials),
            "--n-jobs", str(n_jobs),
            "--seed", str(seed),
        ]
        if pruner in ("asha", "median"):
            cmd += ["--pruner", pruner]

        logger.info("hpo_cli_start", job_id=job_id, cmd=" ".join(cmd), out=str(out_dir))
        # Use subprocess.run in a worker thread for compatibility with existing tests
        # that patch subprocess.run and to keep deterministic stdout/stderr capture.
        cp = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(out_dir),
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        stdout = (cp.stdout or "").encode("utf-8", errors="ignore")
        stderr = (cp.stderr or "").encode("utf-8", errors="ignore")
        rc = int(cp.returncode)
        result["ok"] = (rc == 0)

        # Collect artifacts: best_params file and storage DB if exists
        best_json = out_dir / f"{study_name}_best_params.json"
        if best_json.exists():
            try:
                data = json.loads(best_json.read_text("utf-8"))
                # Expose best_params as-is
                result["metrics"]["best_params"] = data
                # Shadow key for top-level profitability metrics (if present)
                # Common metric names: sharpe, sortino, total_return, mdd/max_drawdown, win_rate, trades, profit_factor
                for k_src, k_dst in [
                    ("sharpe", "sharpe"),
                    ("sortino", "sortino"),
                    ("total_return", "total_return"),
                    ("pnl", "pnl"),
                    ("mdd", "mdd"),
                    ("max_drawdown", "mdd"),
                    ("win_rate", "win_rate"),
                    ("trades", "trades"),
                    ("profit_factor", "profit_factor"),
                ]:
                    try:
                        if k_dst in result["metrics"]:
                            continue
                        if k_src in data and isinstance(data[k_src], (int, float)):
                            result["metrics"][k_dst] = float(data[k_src])
                    except Exception:
                        continue
                result["artifacts"].append(str(best_json))
            except Exception:
                pass
        # If storage points to sqlite file and it's relative, it will be in out_dir
        if storage.startswith("sqlite:///"):
            db_path = storage.replace("sqlite:///", "")
            db_file = (out_dir / db_path)
            if db_file.exists():
                result["artifacts"].append(str(db_file))

        # Attach short tails of logs
        result["stdout"] = (stdout.decode("utf-8", errors="ignore")[-1000:] if stdout else "")
        result["stderr"] = (stderr.decode("utf-8", errors="ignore")[-1000:] if stderr else "")

        if not result["ok"]:
            result["error"] = f"hpo_cli_upgraded exited with {rc}"
            # Attach a simple error_code so Brain can distinguish HPO-level failures
            result["metrics"]["error_code"] = "hpo_cli_failed"
            logger.error("hpo_cli_failed", job_id=job_id, rc=rc, stderr_tail=result["stderr"])
        else:
            logger.info("hpo_cli_completed", job_id=job_id, artifacts=len(result["artifacts"]))

    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.error("hpo_cli_exception", job_id=job_id, error=str(e), exc_info=True)
    return result


async def _execute_bulk_backfill(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute bulk backfill across a date range by invoking the same
    IBKR→ClickHouse pipeline used by DataHealer, instead of the old
    Kafka-only trigger_backfill path.

    Params:
      - symbols: list[str] or comma-separated string
      - start_date: 'YYYY-MM-DD' (inclusive)
      - end_date:   'YYYY-MM-DD' (inclusive)
      - bar_size: '1m'|'5m'|'10m'|'15m'|'30m'|'1h'|'2h'|'3h'|'4h'|'1d' (default '1m')
    """
    import datetime as _dt
    from libs.healer.data_healer_v2 import DataHealerV2
    from libs.health import Action

    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        syms_param = params.get("symbols", [])
        if isinstance(syms_param, str):
            symbols = [s.strip() for s in syms_param.split(",") if s.strip()]
        elif isinstance(syms_param, list):
            symbols = [str(s) for s in syms_param]
        else:
            symbols = []
        if not symbols:
            try:
                from libs.config import config as _cfg2
                symbols = [str(s) for s in (_cfg2.universe or [])]
            except Exception:
                symbols = []
        if not symbols:
            raise RuntimeError("bulk_backfill_no_symbols")

        start_date = params.get("start_date")
        end_date = params.get("end_date")

        default_used = False
        if not start_date or not end_date:
            default_days = int(os.getenv("BULK_BACKFILL_DEFAULT_DAYS", 3))
            ed_date = date.today()
            sd_date = ed_date - timedelta(days=max(0, default_days - 1))
            start_date = sd_date.isoformat()
            end_date = ed_date.isoformat()
            default_used = True

        bar_size = str(params.get("bar_size", "1m"))

        sd = _dt.datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = _dt.datetime.strptime(end_date, "%Y-%m-%d").date()
        if ed < sd:
            raise RuntimeError("end_before_start")

        today = date.today()
        lookback_days = max(1, (today - sd).days + 1)

        healer = DataHealerV2(runs_dir="runs/ai_trader")
        
        action = Action(
            action_id=f"bulk_backfill_{job_id}",
            action_type="backfill_bars",
            priority=1,
            title=f"Bulk backfill for {len(symbols)} symbols",
            description=f"Backfilling from {start_date} to {end_date}",
            params={
                "symbols": symbols,
                "start_date": start_date,
                "end_date": end_date,
                "bar_size": bar_size,
                "lookback_days": lookback_days,
            },
            is_safe_auto_execute=True,
            requires_approval=False
        )

        logger.info(
            "bulk_backfill_healer_start",
            job_id=job_id,
            action_id=action.action_id
        )

        heal_result = healer.heal(action)
        
        if isinstance(heal_result, list):
            final_result = heal_result[0] if heal_result else None
        else:
            final_result = heal_result
            
        if final_result and final_result.success:
            result["ok"] = True
            result["metrics"] = {
                "symbols": symbols,
                "start_date": start_date,
                "end_date": end_date,
                "bar_size": bar_size,
                "lookback_days": lookback_days,
                "rows_affected": final_result.rows_affected,
                "default_window_used": default_used,
            }
        else:
            result["ok"] = False
            result["error"] = final_result.error if final_result else "Unknown healing error"
            result["metrics"] = { "error": result["error"] }

        return result
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.error("bulk_backfill_exception", job_id=job_id, error=str(e), exc_info=True)
        return result


async def _execute_feature_enhanced_backfill(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute enhanced feature backfill (FeatureEngineerV2) by delegating to
    tools/backfill_enhanced_features.py.

    Params:
      - symbols: list[str] or comma-separated string
      - start: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'
      - end:   same as start
      - batch_days: optional int (default 7)
    """
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        syms_param = params.get("symbols", [])
        if isinstance(syms_param, str):
            symbols = [s.strip() for s in syms_param.split(",") if s.strip()]
        elif isinstance(syms_param, list):
            symbols = [str(s) for s in syms_param]
        else:
            symbols = []
        if not symbols:
            raise RuntimeError("feature_enhanced_backfill_no_symbols")

        start = str(params.get("start") or "")
        end = str(params.get("end") or "")
        if not start or not end:
            raise RuntimeError("feature_enhanced_backfill_missing_dates")

        batch_days = int(params.get("batch_days", 7) or 7)

        # Optional: restrict to specific enhanced columns. This allows the
        # Brain or host to "punch holes" for a small set of columns instead of
        # recomputing the entire enhanced feature family.
        only_cols_param = params.get("only_cols")
        only_cols: list[str] = []
        if isinstance(only_cols_param, str):
            only_cols = [c.strip() for c in only_cols_param.split(",") if c.strip()]
        elif isinstance(only_cols_param, list):
            only_cols = [str(c).strip() for c in only_cols_param if str(c).strip()]

        cmd = [
            sys.executable,
            "tools/backfill_enhanced_features.py",
            "--symbols",
            ",".join(symbols),
            "--start",
            start,
            "--end",
            end,
            "--batch-days",
            str(batch_days),
        ]
        if only_cols:
            cmd.extend(["--only-cols", ",".join(only_cols)])

        logger.info(
            "feature_enhanced_backfill_start",
            job_id=job_id,
            cmd=" ".join(cmd),
            symbols=symbols,
            start=start,
            end=end,
            batch_days=batch_days,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        rc = proc.returncode
        result["ok"] = rc == 0
        result["stdout"] = stdout.decode("utf-8", errors="ignore")[-2000:] if stdout else ""
        result["stderr"] = stderr.decode("utf-8", errors="ignore")[-2000:] if stderr else ""
        result["metrics"] = {
            "symbols": symbols,
            "start": start,
            "end": end,
            "batch_days": batch_days,
            "only_cols": only_cols,
            "returncode": rc,
        }
        if not result["ok"]:
            result["error"] = f"backfill_enhanced_features exited with {rc}"
            logger.error(
                "feature_enhanced_backfill_failed",
                job_id=job_id,
                rc=rc,
                stderr_tail=result["stderr"],
            )
        else:
            logger.info(
                "feature_enhanced_backfill_completed",
                job_id=job_id,
                symbols=symbols,
            )
        return result
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.error("feature_enhanced_backfill_exception", job_id=job_id, error=str(e), exc_info=True)
        return result

async def _execute_ensemble_sweep(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Quick sweep over fusion weights to pick best by PF→Sharpe→Return.

    Params:
      - symbols: list[str]
      - start: str 'YYYY-MM-DD HH:MM:SS'
      - end:   str
      - grid: list[float] weights for xgb in (0,1); l=1-w
    """
    result = {"ok": False, "artifacts": [], "metrics": {}}
    try:
        symbols = params.get("symbols") or []
        if not symbols:
            # fallback to enabled symbols from config
            try:
                from libs.config import config as _cfg
                symbols = [str(s) for s in (_cfg.universe or [])]
            except Exception:
                symbols = ["AAPL", "TSLA"]
        start = params.get("start") or (date.today().isoformat() + " 09:30:00")
        end = params.get("end") or (date.today().isoformat() + " 16:00:00")
        grid = params.get("grid") or [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        out_base = _runs_dir() / f"ensemble_sweep_{job_id}"
        out_base.mkdir(parents=True, exist_ok=True)

        def _score(m: dict) -> tuple:
            pf = float(m.get("profit_factor", 0.0) or 0.0)
            sh = float(m.get("sharpe", 0.0) or 0.0)
            ret = float(m.get("total_return", 0.0) or 0.0)
            return (pf, sh, ret)

        best = None
        best_w = None
        artifacts = []
        for w in grid:
            wl = max(0.0, min(1.0, float(w)))
            wr = max(0.0, min(1.0, 1.0 - wl))
            out_dir = out_base / f"w_{int(wl*100):03d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable, "tools/backtest_ensemble_compare.py",
                "--symbols", ",".join(symbols),
                "--start", start, "--end", end,
                "--features-table", "trading.features_1m",
                "--out", str(out_dir),
                "--w_xgb", str(wl), "--w_lstm", str(wr)
            ]
            logger.info("ensemble_sweep_run", job_id=job_id, weights={"xgb": wl, "lstm": wr})
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await p.communicate()
            cmp_json = out_dir / "compare.json"
            if not cmp_json.exists():
                continue
            try:
                data = json.loads(cmp_json.read_text("utf-8"))
                m = data.get("fusion") or {}
                score = _score(m)
                artifacts.append(str(cmp_json))
                if best is None or score > _score(best):
                    best = m
                    best_w = {"xgb": wl, "lstm": wr}
            except Exception:
                continue

        if best is None:
            result["ok"] = False
            result["error"] = "no_results"
        else:
            result["ok"] = True
            result["metrics"] = best
            result["best_weights"] = best_w
            result["artifacts"] = artifacts
    except Exception as e:
        logger.error("ensemble_sweep_failed", job_id=job_id, error=str(e), exc_info=True)
        result["ok"] = False
        result["error"] = str(e)
    return result


async def _execute_feature_build(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run feature build (P0 skeleton)."""
    result = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        out_dir = _runs_dir() / "features_build"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "tools/feature_build.py",
            "--catalog", params.get("catalog", "config/features_catalog.json"),
            "--out", str(out_dir),
        ]
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await p.communicate()
        try:
            payload = json.loads(stdout.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        ok_flag = p.returncode == 0 and bool(payload.get("ok", True))
        err_msg = str(payload.get("error") or "")
        metrics: Dict[str, Any] = {
            "built": payload.get("built", 0),
            "version": payload.get("version"),
        }
        if not ok_flag:
            msg_lower = err_msg.lower()
            if "unknown function" in msg_lower:
                error_code = "feature_build_unknown_function"
            elif "unknown table" in msg_lower:
                error_code = "feature_build_unknown_table"
            elif "db::exception" in msg_lower:
                error_code = "feature_build_clickhouse_error"
            else:
                error_code = "feature_build_failed"
            metrics["error_code"] = error_code
            if err_msg:
                metrics["error_message"] = err_msg[-200:]
        result.update({
            "ok": ok_flag,
            "metrics": metrics,
            "artifacts": [str(out_dir / "status.json")],
            "stderr": stderr.decode("utf-8", errors="ignore")[-1000:],
        })
        return result
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    return result


async def _execute_feature_resample(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Regenerate lower-frequency feature tables (5m/daily) from trading.features_1m."""
    alias_cfg = {
        "m5": {
            "target": params.get("target_m5") or "trading.features_5m",
            "args": [
                "--kind",
                "five_min",
                "--interval",
                str(params.get("interval_minutes", 5) or 5),
            ],
        },
        "d": {
            "target": params.get("target_d") or "trading.features_1d_prev",
            "args": ["--kind", "daily_prev"],
        },
    }
    sources = params.get("sources") or ["m5", "d"]
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    result: Dict[str, Any] = {"ok": True, "metrics": {"aliases": []}, "artifacts": []}
    for alias in sources:
        entry = {"alias": alias, "status": "skipped"}
        cfg = alias_cfg.get(alias)
        if not cfg:
            entry["reason"] = "unknown_alias"
            result["metrics"]["aliases"].append(entry)
            continue
        cmd = [
            sys.executable,
            "tools/feature_resample.py",
            "--source",
            params.get("source") or "trading.features_1m",
            "--target",
            cfg["target"],
            "--replace",
        ] + cfg["args"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            entry["returncode"] = proc.returncode
            entry["stderr_tail"] = (stderr.decode("utf-8", errors="ignore")[-400:] if stderr else "")
            payload: Dict[str, Any] = {}
            try:
                payload = json.loads(stdout.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            entry["status"] = "ok" if proc.returncode == 0 and payload.get("status") == "ok" else "failed"
            entry["details"] = payload or {"note": "no_payload"}
            if entry["status"] != "ok":
                result["ok"] = False
            else:
                result["artifacts"].append(f"{alias}:{cfg['target']}")
        except Exception as exc:
            entry["status"] = "failed"
            entry["reason"] = str(exc)
            result["ok"] = False
        result["metrics"]["aliases"].append(entry)
    return result


async def _execute_label_recompute(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run tools/recompute_labels_1m.py with provided window/threshold."""
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    symbols = params.get("symbols")
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbols:
        symbols = _default_symbols()
    start = params.get("start") or (date.today() - timedelta(days=1)).isoformat() + " 09:30:00"
    end = params.get("end") or date.today().isoformat() + " 16:00:00"

    # Clamp to last data day if available to avoid empty windows
    try:
        from libs.utils.market_calendar import last_data_day
        data_day = last_data_day(symbols)
        if data_day:
            end = f"{data_day} 16:00:00"
            start = (datetime.combine(data_day, datetime.min.time()) - timedelta(days=1)).strftime("%Y-%m-%d 09:30:00")
            result["metrics"]["clamped_to_last_data_day"] = data_day.isoformat()
    except Exception:
        pass

    horizon = int(params.get("horizon_min", 5))
    cmd = [
        sys.executable,
        "tools/recompute_labels_1m.py",
        "--symbols",
        ",".join(symbols),
        "--start",
        start,
        "--end",
        end,
        "--label-col",
        f"label_h{horizon}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        result["stdout"] = stdout.decode("utf-8", errors="ignore")[-2000:]
        result["stderr"] = stderr.decode("utf-8", errors="ignore")[-2000:]
        result["ok"] = proc.returncode == 0
        result["metrics"] = {
            "symbols": symbols,
            "horizon_min": horizon,
            "label_bps": label_bps,
            "returncode": proc.returncode,
        }
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        logger.error("label_recompute_failed", job_id=job_id, error=str(exc), exc_info=True)
    return result


async def _execute_feature_experiment(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run a build+eval cycle using inline catalog specs (sandbox experiment)."""
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    specs = params.get("specs") or []
    if not isinstance(specs, list) or not specs:
        result["error"] = "no_specs"
        return result

    # Prepare run directory
    run_dir = _runs_dir() / "feature_experiments" / job_id
    build_dir = run_dir / "build"
    eval_dir = run_dir / "eval"
    run_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)

    catalog = {
        "version": params.get("version") or job_id,
        "active": [],
        "candidate": specs,
    }
    catalog_path = run_dir / "catalog.json"
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    # Ensure dependent resample sources exist
    required = sorted({src for spec in specs for src in (spec.get("required_sources") or [])})
    if required:
        resample_result = await _execute_feature_resample(f"{job_id}-resample", {"sources": required})
        result.setdefault("metrics", {})["resample"] = resample_result.get("metrics")
        if not resample_result.get("ok"):
            result["error"] = "resample_failed"
            return result

    # Build view using temporary catalog
    cmd_build = [
        sys.executable,
        "tools/feature_build.py",
        "--catalog",
        str(catalog_path),
        "--out",
        str(build_dir),
    ]
    proc_build = await asyncio.create_subprocess_exec(
        *cmd_build,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_build, stderr_build = await proc_build.communicate()
    build_payload: Dict[str, Any] = {}
    try:
        build_payload = json.loads(stdout_build.decode("utf-8") or "{}")
    except Exception:
        build_payload = {}
    if proc_build.returncode != 0 or not build_payload.get("ok", True):
        result["stderr"] = stderr_build.decode("utf-8", errors="ignore")[-1000:]
        result["build_payload"] = build_payload
        result["error"] = "feature_build_failed"
        result["artifacts"].append(str(build_dir / "status.json"))
        return result
    result["artifacts"].append(str(build_dir / "status.json"))

    # Evaluate candidate view
    cmd_eval = [
        sys.executable,
        "tools/feature_eval.py",
        "--catalog",
        str(catalog_path),
        "--out",
        str(eval_dir),
    ]
    proc_eval = await asyncio.create_subprocess_exec(
        *cmd_eval,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_eval, stderr_eval = await proc_eval.communicate()
    eval_payload: Dict[str, Any] = {}
    try:
        eval_payload = json.loads(stdout_eval.decode("utf-8") or "{}")
    except Exception:
        eval_payload = {}

    if proc_eval.returncode != 0 or not eval_payload.get("ok", True):
        result["stderr"] = stderr_eval.decode("utf-8", errors="ignore")[-1000:]
        result["error"] = eval_payload.get("error") or "feature_eval_failed"
        result["artifacts"].append(str(eval_dir))
        return result

    metrics = eval_payload.get("metrics") or {}
    report_files = list(eval_dir.glob("report_*.json"))
    if report_files:
        result["artifacts"].append(str(report_files[0]))
    else:
        result["artifacts"].append(str(eval_dir))

    result["ok"] = True
    result["metrics"] = metrics
    return result

async def _execute_feature_search(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a lightweight feature search round by chaining build → eval.

    Inputs (params):
      - catalog: path to features_catalog.json (default: config/features_catalog.json)
      - out_dir: optional output directory base (defaults under runs/.../feature_search)

    Returns result with ok, metrics from feature_eval report, and artifacts.
    """
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        catalog = str(params.get("catalog") or "config/features_catalog.json")
        base_out = _runs_dir() / "feature_search"
        base_out.mkdir(parents=True, exist_ok=True)

        # Step 1: Build candidate view
        cmd_build = [
            sys.executable,
            "tools/feature_build.py",
            "--catalog", catalog,
            "--out", str(base_out / "build")
        ]
        p1 = await asyncio.create_subprocess_exec(*cmd_build, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out1, err1 = await p1.communicate()
        result["artifacts"].append(str(base_out / "build" / "status.json"))
        if p1.returncode != 0:
            result["ok"] = False
            result["stderr"] = (err1.decode("utf-8", errors="ignore")[-1000:] if err1 else "")
            return result

        # Step 2: Evaluate candidate vs baseline
        cmd_eval = [
            sys.executable,
            "tools/feature_eval.py",
            "--catalog", catalog,
            "--out", str(base_out / "eval")
        ]
        p2 = await asyncio.create_subprocess_exec(*cmd_eval, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out2, err2 = await p2.communicate()
        if p2.returncode != 0:
            result["ok"] = False
            result["stderr"] = (err2.decode("utf-8", errors="ignore")[-1000:] if err2 else "")
            return result
        # Parse report
        import json as _J
        rep = base_out / "eval"
        # pick first report*.json if version is unknown
        report_files = list(rep.glob("report_*.json"))
        if report_files:
            data = _J.loads(report_files[0].read_text("utf-8"))
            result["metrics"] = data.get("metrics", {})
            result["artifacts"].append(str(report_files[0]))
        result["ok"] = True
        return result
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.error("feature_search_exception", job_id=job_id, error=str(e), exc_info=True)
        return result

async def _execute_feature_ablation(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute feature ablation (single-removal trials)."""
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        catalog = str(params.get("catalog") or "config/features_catalog.json")
        max_trials = str(params.get("max_trials", 5))
        out_dir = _runs_dir() / "feature_ablation"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "tools/feature_ablation.py",
            "--catalog", catalog,
            "--out", str(out_dir),
            "--max-trials", str(max_trials),
        ]
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await p.communicate()
        try:
            payload = json.loads(stdout.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        result.update({
            "ok": p.returncode == 0 and bool(payload.get("ok", True)),
            "metrics": payload,
            "artifacts": [str(out_dir / "ablation_report.json")],
            "stderr": stderr.decode("utf-8", errors="ignore")[-1000:],
        })
        return result
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.error("feature_ablation_exception", job_id=job_id, error=str(e), exc_info=True)
        return result


def _default_symbols() -> List[str]:
    try:
        symbols = (_cfg.get("ml") or {}).get("symbols")
        if symbols:
            return [str(s) for s in symbols]
    except Exception:
        pass
    try:
        from libs.config import config as _global_cfg
        symbols = _global_cfg.universe or []
        if symbols:
            return [str(s) for s in symbols]
    except Exception:
        pass
    return ["TSLA"]

async def _execute_label_diagnostics(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Compute simple label diagnostics over features_1m_with_labels.

    Params:
      - symbols: list or comma-separated string
      - start: ISO timestamp (inclusive)
      - end: ISO timestamp (exclusive)
      - out_table: optional table name (default trading.features_1m_with_labels)
    """
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        try:
            import clickhouse_connect  # type: ignore
        except ImportError as exc:
            result["error"] = f"clickhouse_connect_missing:{exc}"
            return result

        from libs.config import config as _global_cfg

        syms_param = params.get("symbols") or []
        if isinstance(syms_param, str):
            symbols = [s.strip() for s in syms_param.split(",") if s.strip()]
        elif isinstance(syms_param, list):
            symbols = [str(s) for s in syms_param]
        else:
            symbols = []
        if not symbols:
            symbols = _default_symbols()

        start = params.get("start") or (date.today().isoformat() + " 09:30:00")
        end = params.get("end") or (date.today().isoformat() + " 16:00:00")
        out_table = params.get("out_table") or "trading.features_1m_with_labels"

        ch_cfg = _global_cfg.clickhouse_config
        cli = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=int(ch_cfg.get("port", 8123)),
            username=ch_cfg.get("user", "default"),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "trading"),
        )

        q = f"""
        -- Determine label column dynamically to align with table schema
        SELECT name
        FROM system.columns
        WHERE database = 'trading'
          AND table = '{out_table.split('.')[-1]}'
          AND name LIKE 'label_%'
        ORDER BY name
        LIMIT 1
        """
        label_rows = cli.query(q)
        label_col = "label_h30"
        try:
            # clickhouse_connect returns QueryResult; fast-path named_results if present
            if hasattr(label_rows, "named_results"):
                named = list(label_rows.named_results())
                if named:
                    first_row = named[0]
                    if isinstance(first_row, dict):
                        label_col = first_row.get("name", label_col)
                    elif isinstance(first_row, (list, tuple)) and first_row:
                        label_col = first_row[0] or label_col
            elif isinstance(label_rows, list) and label_rows:
                first_row = label_rows[0]
                if isinstance(first_row, dict):
                    label_col = first_row.get("name", label_col)
                elif isinstance(first_row, (list, tuple)) and first_row:
                    label_col = first_row[0] or label_col
        except Exception:
            # Fall back silently to default label_h30
            pass

        stats_query = f"""
        SELECT
            symbol,
            count() AS total_rows,
            countIf({label_col} > 0) AS positives,
            countIf({label_col} <= 0) AS negatives,
            countIf(isNull({label_col})) AS null_labels,
            avgOrNull({label_col}) AS avg_ret,
            avgOrNull(abs({label_col})) AS avg_abs_ret
        FROM {out_table}
        WHERE symbol IN %(syms)s
          AND ts >= toDateTime(%(start)s)
          AND ts < toDateTime(%(end)s)
        GROUP BY symbol
        """
        stats_res = cli.query(stats_query, parameters={"syms": symbols, "start": start, "end": end})
        rows = list(stats_res.named_results()) if hasattr(stats_res, "named_results") else stats_res
        per_symbol: List[Dict[str, Any]] = []
        zero_frac_by_symbol: Dict[str, float] = {}
        for r in rows:
            s = dict(r)
            per_symbol.append(s)
            tot = float(s.get("total_rows") or 0.0)
            nulls = float(s.get("null_labels") or 0.0)
            zero_frac_by_symbol[str(s.get("symbol"))] = (nulls / tot) if tot > 0 else 0.0

        result["ok"] = True
        result["metrics"] = {
            "per_symbol": per_symbol,
            "zero_frac_by_symbol": zero_frac_by_symbol,
            "symbols": symbols,
            "window": {"start": start, "end": end},
        }
        return result
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        logger.error("label_diagnostics_failed", job_id=job_id, error=str(exc), exc_info=True)
        return result

async def _execute_feature_ablation(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute feature ablation (single-removal trials)."""
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        catalog = str(params.get("catalog") or "config/features_catalog.json")
        max_trials = str(params.get("max_trials", 5))
        out_dir = _runs_dir() / "feature_ablation"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "tools/feature_ablation.py",
            "--catalog", catalog,
            "--out", str(out_dir),
            "--max-trials", str(max_trials),
        ]
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await p.communicate()
        try:
            payload = json.loads(stdout.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        result.update({
            "ok": p.returncode == 0 and bool(payload.get("ok", True)),
            "metrics": payload,
            "artifacts": [str(out_dir / "ablation_report.json")],
            "stderr": stderr.decode("utf-8", errors="ignore")[-1000:],
        })
        return result
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.error("feature_ablation_exception", job_id=job_id, error=str(e), exc_info=True)
        return result


def _default_symbols() -> List[str]:
    try:
        symbols = (_cfg.get("ml") or {}).get("symbols")
        if symbols:
            return [str(s) for s in symbols]
    except Exception:
        pass
    try:
        from libs.config import config as _global_cfg
        symbols = _global_cfg.universe or []
        if symbols:
            return [str(s) for s in symbols]
    except Exception:
        pass
    return ["TSLA"]

async def _execute_label_diagnostics(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Compute simple label diagnostics over features_1m_with_labels.

    Params:
      - symbols: list or comma-separated string
      - start: ISO timestamp (inclusive)
      - end: ISO timestamp (exclusive)
      - out_table: optional table name (default trading.features_1m_with_labels)
    """
    result: Dict[str, Any] = {"ok": False, "metrics": {}, "artifacts": []}
    try:
        try:
            import clickhouse_connect  # type: ignore
        except ImportError as exc:
            result["error"] = f"clickhouse_connect_missing:{exc}"
            return result

        from libs.config import config as _global_cfg

        syms_param = params.get("symbols") or []
        if isinstance(syms_param, str):
            symbols = [s.strip() for s in syms_param.split(",") if s.strip()]
        elif isinstance(syms_param, list):
            symbols = [str(s) for s in syms_param]
        else:
            symbols = []
        if not symbols:
            symbols = _default_symbols()

        start = params.get("start") or (date.today().isoformat() + " 09:30:00")
        end = params.get("end") or (date.today().isoformat() + " 16:00:00")
        out_table = params.get("out_table") or "trading.features_1m_with_labels"

        ch_cfg = _global_cfg.clickhouse_config
        cli = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=int(ch_cfg.get("port", 8123)),
            username=ch_cfg.get("user", "default"),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "trading"),
        )

        q = f"""
        -- Determine label column dynamically to align with table schema
        SELECT name
        FROM system.columns
        WHERE database = 'trading'
          AND table = '{out_table.split('.')[-1]}'
          AND name LIKE 'label_%'
        ORDER BY name
        LIMIT 1
        """
        label_rows = cli.query(q)
        label_col = "label_h30"
        try:
            # clickhouse_connect returns QueryResult; fast-path named_results if present
            if hasattr(label_rows, "named_results"):
                named = list(label_rows.named_results())
                if named:
                    first_row = named[0]
                    if isinstance(first_row, dict):
                        label_col = first_row.get("name", label_col)
                    elif isinstance(first_row, (list, tuple)) and first_row:
                        label_col = first_row[0] or label_col
            elif isinstance(label_rows, list) and label_rows:
                first_row = label_rows[0]
                if isinstance(first_row, dict):
                    label_col = first_row.get("name", label_col)
                elif isinstance(first_row, (list, tuple)) and first_row:
                    label_col = first_row[0] or label_col
        except Exception:
            # Fall back silently to default label_h30
            pass

        stats_query = f"""
        SELECT
            symbol,
            count() AS total_rows,
            countIf({label_col} > 0) AS positives,
            countIf({label_col} <= 0) AS negatives,
            countIf(isNull({label_col})) AS null_labels,
            avgOrNull({label_col}) AS avg_ret,
            avgOrNull(abs({label_col})) AS avg_abs_ret
        FROM {out_table}
        WHERE symbol IN %(syms)s
          AND ts >= toDateTime(%(start)s)
          AND ts < toDateTime(%(end)s)
        GROUP BY symbol
        """
        stats_res = cli.query(stats_query, parameters={"syms": symbols, "start": start, "end": end})
        rows = list(stats_res.named_results()) if hasattr(stats_res, "named_results") else stats_res
        per_symbol: List[Dict[str, Any]] = []
        zero_frac_by_symbol: Dict[str, float] = {}
        for r in rows:
            s = dict(r)
            per_symbol.append(s)
            tot = float(s.get("total_rows") or 0.0)
            nulls = float(s.get("null_labels") or 0.0)
            zero_frac_by_symbol[str(s.get("symbol"))] = (nulls / tot) if tot > 0 else 0.0

        result["ok"] = True
        result["metrics"] = {
            "per_symbol": per_symbol,
            "zero_frac_by_symbol": zero_frac_by_symbol,
            "symbols": symbols,
            "window": {"start": start, "end": end},
        }
        return result
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        logger.error("label_diagnostics_failed", job_id=job_id, error=str(exc), exc_info=True)
        return result

class MLScheduler:
    def __init__(self):
        self.running = False
        self.job_complete_event = asyncio.Event()

    async def process_queue(self, max_iterations: Optional[int] = None):
        """Main scheduler loop - processes queue in priority order.

        Args:
            max_iterations: Optional limit for testing (None = run forever)
        """
        self.running = True
        iterations = 0

        logger.info("ml_scheduler_started")

        # Requeue stale running jobs on startup
        _requeue_stale_running_jobs(max_age_sec=600)

        try:
            while self.running:
                # Requeue stale running jobs at the start of each iteration
                _requeue_stale_running_jobs(max_age_sec=600)
                # Recover orphaned slot lock if present (no running job + age > threshold)
                try:
                    _recover_orphaned_slot_lock(orphan_age_sec=int(((_cfg.get("policies") or {}).get("ml") or {}).get("slot_orphan_sec", 240)))
                except Exception as e:
                    logger.warning("recover_orphaned_lock_failed", error=str(e))

                # Check if we should run (quiet hours, etc.)
                if self._is_quiet_hours():
                    logger.debug("quiet_hours_active", action="skip")
                    await asyncio.sleep(300)  # 5 min
                    continue

                # Check slot availability
                if not _acquire_slot():
                    logger.debug("ml_slot_busy", action="wait")
                    await asyncio.sleep(60)  # 1 min
                    continue

                try:
                    # Load queue
                    jobs = load_queue()
                    if not jobs:
                        logger.debug("queue_empty", action="wait")
                        await asyncio.sleep(300)  # 5 min
                        continue

                    # Get highest priority job
                    job = jobs[0]
                    logger.info("job_dequeued", job_id=job["job_id"], job_type=job["job_type"],
                               priority=job["priority"], queue_size=len(jobs))

                    # Memory guard check
                    est_mem = job.get("est_mem_gb", 4.0)
                    if not self._check_memory_available(est_mem):
                        logger.warning("insufficient_memory", job_id=job["job_id"],
                                     est_mem_gb=est_mem, action="requeue")
                        await asyncio.sleep(300)
                        continue

                    # Execute job with timeout guard
                    timeout_sec = int(((_cfg.get("policies") or {}).get("ml") or {}).get("job_timeout_sec", 600))
                    try:
                        result = await asyncio.wait_for(execute_job(job), timeout=timeout_sec)
                        logger.info("job_completed", job_id=job["job_id"], ok=result["ok"],
                                   duration_sec=result["duration_sec"])
                    except asyncio.TimeoutError:
                        # Job execution timed out
                        mark_job_status(job["job_id"], "failed")
                        _publish_ml_event({
                            "event": "ml.job",
                            "status": "failed",
                            "job_id": job["job_id"],
                            "job_type": job["job_type"],
                            "error_type": "timeout",
                            "error_message": f"Job execution timeout after {timeout_sec}s"
                        })
                        logger.error("job_execution_timeout",
                                   job_id=job["job_id"],
                                   job_type=job["job_type"],
                                   timeout_sec=timeout_sec)
                        # Continue to next job (slot will be released in finally block)

                finally:
                    # Always release slot
                    _release_slot()

                iterations += 1
                if max_iterations and iterations >= max_iterations:
                    break

                # Brief pause before next job
                await asyncio.sleep(10)

        except KeyboardInterrupt:
            logger.info("ml_scheduler_interrupted")
        except Exception as e:
            logger.error("ml_scheduler_error", error=str(e), exc_info=True)
        finally:
            self.running = False
            _release_slot()
            logger.info("ml_scheduler_stopped")

    def _is_quiet_hours(self) -> bool:
        """Check if current time is in quiet hours (02:00-06:00 ET)."""
        try:
            quiet_cfg = (_cfg.get("ml", {}) or {}).get("quiet_hours_et", ["02:00", "06:00"])
            # TODO: Implement time zone conversion
            return False  # Simplified for now
        except Exception:
            return False

    def _check_memory_available(self, est_mem_gb: float) -> bool:
        """Check if enough memory is available.

        Args:
            est_mem_gb: Estimated memory requirement

        Returns:
            True if memory available
        """
        try:
            import psutil
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024**3)
            threshold = 0.85  # 85% usage limit

            if mem.percent > threshold * 100:
                logger.warning("memory_pressure", available_gb=available_gb,
                             usage_percent=mem.percent, threshold=threshold)
                return False

            if available_gb < est_mem_gb:
                logger.warning("insufficient_memory_for_job", available_gb=available_gb,
                             required_gb=est_mem_gb)
                return False

            return True

        except ImportError:
            logger.warning("psutil_not_available", action="assume_ok")
            return True


# ============================================================================ 
# CLI
# ============================================================================ 

def _cleanup_stale_locks():
    """Clean up stale lock files on startup.

    When the scheduler is killed (SIGKILL, crash, etc), lock files may be left behind.
    This function removes all lock files on startup to prevent deadlocks.
    """
    try:
        cleaned = []
        # Consider both non-dated and dated lock directories
        base_dir = _runs_dir() / "locks"
        dated_dir = _runs_dir() / date.today().isoformat() / "locks"
        for locks_dir in [base_dir, dated_dir]:
            if not locks_dir.exists():
                continue
            for lock_file in locks_dir.glob("*.lock"):
                try:
                    # Check if it's ml_slot.lock or other critical locks
                    lock_name = lock_file.name

                    # Read lock content to log what we're cleaning
                    try:
                        lock_content = lock_file.read_text().strip()
                    except Exception:
                        lock_content = "<unreadable>"

                    # Remove the lock
                    lock_file.unlink()
                    cleaned.append(lock_name)
                    logger.info("stale_lock_cleaned", lock=lock_name, content=lock_content,
                               reason="startup_cleanup")
                except Exception as e:
                    logger.warning("lock_cleanup_failed", lock=lock_file.name, error=str(e))

        if cleaned:
            logger.info("startup_lock_cleanup_complete", locks_cleaned=len(cleaned), locks=cleaned)
        else:
            logger.debug("no_stale_locks_found")

    except Exception as e:
        logger.warning("lock_cleanup_error", error=str(e), exc_info=True)


async def main():
    """Run scheduler from command line."""
    # ✅ Optional: force replace existing scheduler (env flag)
    try:
        import os, signal, time
        force = str(os.getenv("ML_SCHEDULER_FORCE_REPLACE", "")).lower() in {"1","true","yes","on"}
        if force:
            pf = _scheduler_pid_file()
            if pf.exists():
                try:
                    pid = int(pf.read_text().strip())
                    # Check if process is alive and looks like our scheduler
                    import subprocess
                    procs = subprocess.run(["pgrep","-f","ml_scheduler.py"], capture_output=True, text=True, timeout=2)
                    running_pids = {int(x) for x in procs.stdout.split() if x.strip().isdigit()} if procs.returncode==0 else set()
                    if pid in running_pids:
                        # Graceful terminate, then kill -9 if needed
                        try:
                            os.kill(pid, signal.SIGTERM)
                            time.sleep(3)
                        except Exception:
                            pass
                        # Re-check and SIGKILL if still alive
                        procs2 = subprocess.run(["pgrep","-f","ml_scheduler.py"], capture_output=True, text=True, timeout=2)
                        running2 = {int(x) for x in procs2.stdout.split() if x.strip().isdigit()} if procs2.returncode==0 else set()
                        if pid in running2:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except Exception:
                                pass
                        logger.info("scheduler_force_replaced", pid=pid)
                except Exception as e:
                    logger.warning("scheduler_force_replace_failed", error=str(e))
            # Clean locks unconditionally in force mode
            _cleanup_stale_locks()
        else:
            # ✅ Clean up stale locks from previous runs (killed processes, crashes)
            _cleanup_stale_locks()
    except Exception:
        _cleanup_stale_locks()

    # ✅ Acquire singleton lock to prevent multiple instances
    if not _acquire_singleton_lock():
        # Another instance is already running
        logger.info("ml_scheduler_exit", reason="another_instance_running")
        return

    scheduler = MLScheduler()
    # Write PID file for watchdog/process visibility (best-effort)
    try:
        pf = _scheduler_pid_file()
        pf.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    await scheduler.process_queue()


if __name__ == "__main__":
    asyncio.run(main())
