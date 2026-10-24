"""
Alpha Loop Wrapper with Memory Guard and Degradation
----------------------------------------------------
Wraps alpha_loop.py execution with:
- Memory watchdog
- Graceful degradation on OOM
- Thread/concurrency control
- Failure capture and recovery

Usage:
    from utils.alpha_loop_wrapper import run_alpha_loop_safe

    result = run_alpha_loop_safe(
        config_path="alpha_config_enhanced.json",
        out_dir="alpha_out/daily",
        gens=2,
        k_top=10,
        provider="coop_optuna",
        round_idx=0,
        max_mem_gb=14.0
    )
"""

import os
import sys
import json
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional
import structlog
import yaml

from utils.mem_guard import MemoryGuard, MemoryBudgetExceeded
import psutil
from utils.degrade_ladder import DegradeLadder

logger = structlog.get_logger(__name__)


def _load_hpo_budget() -> Dict[str, Any]:
    """Load hpo_budget from config.yml."""
    try:
        config_path = Path("config.yml")
        if config_path.exists():
            with open(config_path, 'r') as f:
                cfg = yaml.safe_load(f)
                return cfg.get("policies", {}).get("hpo_budget", {})
    except Exception as e:
        logger.warning("failed_to_load_hpo_budget", error=str(e))

    # Return defaults if config load fails
    return {
        "mem_budget_gb": 14.0,
        "symbol_top_k": 1,
        "trial_limits": 10,
        "window_days": 31,
        "max_total_grid_points": 200,
    }


class AlphaLoopFailure(Exception):
    """Raised when alpha_loop fails."""
    def __init__(self, message: str, details: Dict):
        super().__init__(message)
        self.details = details


def run_alpha_loop_safe(
    config_path: str,
    out_dir: str,
    gens: int,
    k_top: int,
    provider: str,
    round_idx: int = 0,
    max_mem_gb: Optional[float] = None,
    seed: Optional[int] = None
) -> Dict[str, Any]:
    """
    Run alpha_loop with memory guard and degradation support.

    Args:
        config_path: Path to alpha config JSON
        out_dir: Output directory
        gens: Number of generations
        k_top: Top-K rows to guide refinement
        provider: HPO provider (coop_optuna, optuna, etc.)
        round_idx: Current retry round (0-based)
        max_mem_gb: Memory budget in GB (if None, read from config.yml)
        seed: Random seed (optional)

    Returns:
        Dict with status and results

    Raises:
        AlphaLoopFailure: If alpha_loop fails
    """
    # Load budget from config if not specified
    budget = _load_hpo_budget()
    if max_mem_gb is None:
        max_mem_gb = budget.get("mem_budget_gb", 14.0)

    # Initialize memory guard
    mem_guard = MemoryGuard(max_mem_gb=max_mem_gb)

    # PREFLIGHT CHECK: Estimate workload and apply degradation BEFORE Round 0
    preflight_degraded = False
    ladder = DegradeLadder()

    # Load config to estimate workload
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)

        hpo = cfg.get('hpo', {})
        n_symbols = len(hpo.get('symbols', []))

        # Estimate folds from date range
        fold_gen = hpo.get('fold_generator', {})
        is_days = fold_gen.get('is_days', 10)
        oos_days = fold_gen.get('oos_days', 5)
        step_days = fold_gen.get('step_days', 10)

        # Parse dates
        from datetime import datetime
        start = datetime.fromisoformat(fold_gen.get('start', '2025-08-01 09:30:00'))
        end = datetime.fromisoformat(fold_gen.get('end', '2025-08-31 16:00:00'))
        total_days = (end - start).days
        n_folds = max(1, (total_days - is_days - oos_days) // step_days + 1)

        # Get trials
        n_trials = cfg.get('optuna', {}).get('trials', 40)

        # Estimate total training runs
        expected_runs = n_symbols * n_folds * n_trials

        logger.info(
            "preflight_check",
            symbols=n_symbols,
            folds=n_folds,
            trials=n_trials,
            expected_runs=expected_runs,
            total_days=total_days
        )

        # POLICY: ALWAYS apply Level 1 degradation at Round 0 (don't wait for failure)
        # This ensures sustainable baseline from the start
        # Gate threshold from config.yml budget
        symbol_top_k = budget.get("symbol_top_k", 1)
        trial_limits = budget.get("trial_limits", 10)
        window_days = budget.get("window_days", 31)
        max_grid_points = budget.get("max_total_grid_points", 200)

        # Calculate sustainable threshold based on budget
        MAX_RUNS_GATE = symbol_top_k * 4 * trial_limits * 3  # symbols × folds × trials × safety_factor

        if round_idx == 0:
            # Force Level 1 degradation for Round 0 unconditionally
            logger.warning(
                "round_0_forced_degradation",
                expected_runs=expected_runs,
                gate=MAX_RUNS_GATE,
                budget_symbol_top_k=symbol_top_k,
                budget_trial_limits=trial_limits,
                budget_window_days=window_days,
                action="applying_level_1_degradation_at_round_0"
            )
            degrade_cfg = ladder.apply(1, failure_reason="round_0_policy")
            preflight_degraded = True
        elif expected_runs > MAX_RUNS_GATE:
            logger.warning(
                "preflight_workload_too_large",
                expected_runs=expected_runs,
                gate=MAX_RUNS_GATE,
                action="applying_degradation_at_round"
            )
            # Apply appropriate level based on round_idx
            degrade_cfg = ladder.apply(min(round_idx + 1, 3), failure_reason="preflight_workload_gate")
            preflight_degraded = True
        else:
            degrade_cfg = ladder.apply(round_idx, failure_reason="retry_round")

    except Exception as e:
        logger.warning("preflight_check_failed", error=str(e))
        degrade_cfg = ladder.apply(round_idx, failure_reason="retry_round")

    logger.info(
        "alpha_loop_safe_start",
        config=config_path,
        round=round_idx,
        provider=provider,
        gens=gens,
        max_mem_gb=max_mem_gb,
        degrade_level=degrade_cfg["level"],
        preflight_degraded=preflight_degraded
    )

    # Set environment variables for threading
    env = os.environ.copy()
    env_overrides = ladder.get_env_overrides(degrade_cfg)

    # FORCE conservative threading for Round 0
    if round_idx == 0:
        env_overrides["OMP_NUM_THREADS"] = "2"
        env_overrides["MKL_NUM_THREADS"] = "2"
        env_overrides["OPENBLAS_NUM_THREADS"] = "2"
        env_overrides["NUMEXPR_NUM_THREADS"] = "2"
        logger.info("round_0_forced_conservative_threads", threads=2)

    env.update(env_overrides)

    if seed is not None:
        env["PYTHONHASHSEED"] = str(seed)

    logger.info(
        "env_overrides_applied",
        round=round_idx,
        **env_overrides
    )

    # Adjust gens if degraded
    if degrade_cfg.get("gens") is not None:
        gens = min(gens, degrade_cfg["gens"])
        logger.info("gens_degraded", round=round_idx, gens=gens)

    # Build command
    cmd = [
        "venv/bin/python",
        "alpha_loop.py",
        "--config", config_path,
        "--out_dir", out_dir,
        "--gens", str(gens),
        "--k-top", str(k_top),
        "--provider", provider
    ]

    logger.info(
        "alpha_loop_command",
        round=round_idx,
        cmd=" ".join(cmd)
    )

    # Check memory before starting
    mem_guard.check("before_alpha_loop", round_idx=round_idx)

    # Helper: sum RSS (bytes) of a process tree (proc + children)
    def _tree_rss_bytes(pid: int) -> int:
        try:
            proc = psutil.Process(pid)
            rss = proc.memory_info().rss
            for ch in proc.children(recursive=True):
                try:
                    rss += ch.memory_info().rss
                except Exception:
                    continue
            return int(rss)
        except Exception:
            return 0

    # Watcher thread: monitor child memory and enforce budget, emit heartbeats
    stop_flag = {"stop": False}

    # Track simple heartbeat so users can see progress while alpha_loop runs
    start_ts = time.time()
    heartbeat = {"last": 0.0}

    def _watch_child(p: subprocess.Popen):
        warn_mb = (mem_guard.max_mem_bytes * mem_guard.warn_threshold) / (1024 * 1024)
        while not stop_flag["stop"] and p.poll() is None:
            rss_bytes = _tree_rss_bytes(p.pid)
            if rss_bytes:
                rss_mb = rss_bytes / (1024 * 1024)
                # Update peak in guard for accurate reporting
                try:
                    mem_guard.peak_rss_mb = max(mem_guard.peak_rss_mb, rss_mb)
                except Exception:
                    pass
                # Lightweight heartbeat every few seconds so operators can see that HPO is alive
                now = time.time()
                if now - heartbeat["last"] >= 5.0:
                    heartbeat["last"] = now
                    logger.info(
                        "alpha_loop_progress",
                        round=round_idx,
                        rss_mb=round(rss_mb, 1),
                        elapsed_sec=int(now - start_ts),
                    )
                if rss_bytes >= mem_guard.max_mem_bytes:
                    logger.error(
                        "alpha_loop_child_mem_exceeded",
                        round=round_idx,
                        rss_mb=round(rss_mb, 1),
                        max_mem_mb=round(mem_guard.max_mem_bytes / (1024 * 1024), 1)
                    )
                    try:
                        # Terminate the whole group to avoid zombie workers
                        try:
                            pgid = os.getpgid(p.pid)
                            os.killpg(pgid, signal.SIGTERM)
                        except Exception:
                            p.terminate()
                        time.sleep(3)
                        if p.poll() is None:
                            try:
                                pgid = os.getpgid(p.pid)
                                os.killpg(pgid, signal.SIGKILL)
                            except Exception:
                                p.kill()
                    except Exception:
                        pass
                    break
                elif rss_mb >= warn_mb:
                    logger.warning(
                        "alpha_loop_child_mem_warning",
                        round=round_idx,
                        rss_mb=round(rss_mb, 1),
                        warn_threshold_mb=round(warn_mb, 1)
                    )
            time.sleep(1.0)

    # Run alpha_loop as subprocess (new process group for clean signaling)
    try:
        p = subprocess.Popen(
            cmd,
            cwd=os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True  # enable killpg
        )

        watcher = threading.Thread(target=_watch_child, args=(p,), daemon=True)
        watcher.start()

        try:
            stdout, stderr = p.communicate(timeout=3600)  # 60 min timeout
        except subprocess.TimeoutExpired:
            logger.error("alpha_loop_timeout", round=round_idx)
            try:
                pgid = os.getpgid(p.pid)
                os.killpg(pgid, signal.SIGTERM)
                time.sleep(5)
                if p.poll() is None:
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            raise AlphaLoopFailure(
                f"Alpha loop timed out after 60 minutes at round {round_idx}",
                details={"round": round_idx, "timeout": 3600}
            )
        finally:
            stop_flag["stop"] = True
            try:
                watcher.join(timeout=2)
            except Exception:
                pass

        returncode = p.returncode

        # Check memory after
        mem_guard.check("after_alpha_loop", round_idx=round_idx)

        if returncode == 0:
            logger.info(
                "alpha_loop_success",
                round=round_idx,
                returncode=0,
                mem_stats=mem_guard.get_stats()
            )

            # Parse output to extract results
            return {
                "status": "success",
                "round": round_idx,
                "out_dir": out_dir,
                "mem_stats": mem_guard.get_stats(),
                "degrade_level": degrade_cfg["level"],
                "stdout": stdout[-1000:] if stdout else "",
                "stderr": stderr[-1000:] if stderr else ""
            }

        elif returncode == -signal.SIGKILL:
            # SIGKILL (OOM)
            logger.error(
                "alpha_loop_sigkill",
                round=round_idx,
                mem_stats=mem_guard.get_stats()
            )

            raise AlphaLoopFailure(
                f"Alpha loop killed by SIGKILL (OOM or external kill) at round {round_idx}",
                details={
                    "round": round_idx,
                    "signal": "SIGKILL",
                    "mem_stats": mem_guard.get_stats(),
                    "degrade_level": degrade_cfg["level"],
                    "stderr": stderr[-2000:] if stderr else ""
                }
            )

        else:
            # Other failure, including potential signal-based crashes (e.g. SIGSEGV)
            crash_signal = None
            if returncode < 0:
                try:
                    crash_signal = -returncode
                except Exception:
                    crash_signal = None

            # LOG FULL STDERR FOR DIAGNOSIS
            full_stderr = stderr if stderr else ""
            full_stdout = stdout if stdout else ""

            logger.error(
                "alpha_loop_failed",
                round=round_idx,
                returncode=returncode,
                signal=crash_signal,
                stderr_preview=full_stderr[-500:] if full_stderr else ""
            )

            # Log full stderr separately for diagnosis
            if full_stderr:
                logger.error(
                    "alpha_loop_stderr_full",
                    round=round_idx,
                    stderr=full_stderr[-5000:]  # Last 5000 chars
                )

            if full_stdout:
                logger.info(
                    "alpha_loop_stdout_tail",
                    round=round_idx,
                    stdout=full_stdout[-2000:]  # Last 2000 chars
                )

            raise AlphaLoopFailure(
                f"Alpha loop failed with return code {returncode}",
                details={
                    "round": round_idx,
                    "returncode": returncode,
                    "stderr": full_stderr[-2000:],
                    "stdout": full_stdout[-1000:]
                }
            )

    except MemoryBudgetExceeded as e:
        logger.error(
            "alpha_loop_mem_guard_triggered",
            round=round_idx,
            mem_stats=e.stats
        )
        raise AlphaLoopFailure(
            f"Memory budget exceeded at round {round_idx}",
            details={
                "round": round_idx,
                "mem_stats": e.stats,
                "trigger": "mem_guard"
            }
        )

    except Exception as e:
        logger.error(
            "alpha_loop_unexpected_error",
            round=round_idx,
            error=str(e),
            exc_info=True
        )
        raise AlphaLoopFailure(
            f"Unexpected error in alpha loop at round {round_idx}: {str(e)}",
            details={"round": round_idx, "error": str(e)}
        )


def apply_config_degradation(
    config_path: str,
    output_path: str,
    degrade_cfg: Dict[str, Any],
    round_idx: int = 0
) -> None:
    """
    Apply degradation to alpha config and save to new file.

    Args:
        config_path: Original config path
        output_path: Path to save degraded config
        degrade_cfg: Degradation configuration from DegradeLadder
        round_idx: Current round number
    """
    with open(config_path, "r") as f:
        cfg = json.load(f)

    ladder = DegradeLadder()
    budget = _load_hpo_budget()

    # Apply degradation to HPO section
    if "hpo" in cfg:
        cfg["hpo"] = ladder.apply_to_hpo_config(cfg["hpo"], degrade_cfg)

    # FORCE sustainable baseline for Round 0 (from config.yml budget)
    if round_idx == 0:
        hpo = cfg.get("hpo", {})

        # Force symbol limit from budget
        symbol_top_k = budget.get("symbol_top_k", 1)
        if len(hpo.get("symbols", [])) > symbol_top_k:
            hpo["symbols"] = hpo["symbols"][:symbol_top_k]
            logger.info("round_0_forced_symbol_limit", symbols=hpo["symbols"], limit=symbol_top_k)

        # Force trial limit from budget
        trial_limits = budget.get("trial_limits", 10)
        if "optuna" in cfg:
            original_trials = cfg["optuna"].get("trials", 40)
            if original_trials > trial_limits:
                cfg["optuna"]["trials"] = trial_limits
                logger.info("round_0_forced_trials", from_trials=original_trials, to_trials=trial_limits)

        # Force window_days from budget (adjust both hpo and fold_generator dates)
        window_days = budget.get("window_days", 31)
        if "fold_generator" in hpo:
            from datetime import datetime, timedelta
            fold_gen = hpo["fold_generator"]
            try:
                end_dt = datetime.fromisoformat(fold_gen.get("end", "2025-08-31 16:00:00"))
                new_start = end_dt - timedelta(days=window_days)
                new_start_str = new_start.isoformat()

                # Update fold_generator.start
                fold_gen["start"] = new_start_str

                # CRITICAL: Also update hpo.start to match
                hpo["start"] = new_start_str

                logger.info("round_0_forced_window", window_days=window_days, new_start=new_start_str, updated_both=True)
            except Exception as e:
                logger.warning("failed_to_adjust_window", error=str(e))

    # Apply grid size limits to guard config (from budget or degrade_cfg)
    if "guard" not in cfg:
        cfg["guard"] = {}

    max_grid_points = budget.get("max_total_grid_points", 200)
    cfg["guard"]["max_total_points"] = degrade_cfg.get("grid_max_points", max_grid_points)

    # ADDITIONAL: Cap param values per parameter
    cfg["guard"]["cap_per_param"] = 3  # Max 3 values per parameter

    # Prune grid BEFORE saving
    if "hpo" in cfg and "grid" in cfg["hpo"]:
        from grid_guard import sanitize_and_constrain_grid

        original_grid = cfg["hpo"]["grid"]
        pruned_grid, report = sanitize_and_constrain_grid(original_grid, cfg["guard"])

        cfg["hpo"]["grid"] = pruned_grid

        if report.get("combinations_pruned"):
            logger.info(
                "grid_pruned_before_run",
                before_combinations=report.get("total_combinations_before"),
                after_combinations=report.get("total_combinations_after"),
                pruned_count=report.get("pruned_count", 0)
            )

    # Model rotation: force single model per round if configured
    try:
        rotation = budget.get("model_rotation") or []
        if rotation and isinstance(rotation, list):
            # Pick model by round (clamped)
            model = rotation[min(round_idx, len(rotation) - 1)]
            hpo = cfg.setdefault("hpo", {})
            space = hpo.setdefault("optuna_space", {})
            mt = space.setdefault("model_type", {"type": "categorical", "choices": []})
            mt["type"] = "categorical"
            mt["choices"] = [model]

            # Optionally filter model_params choices by family for safety
            mp = space.get("model_params") or {}
            choices = (mp.get("choices") if isinstance(mp, dict) else None) or []
            if choices:
                xgb_keys = {"eta","learning_rate","max_depth","min_child_weight","subsample","colsample_bytree","gamma","lambda","alpha","reg_lambda","reg_alpha","n_estimators","num_boost_round","early_stopping_rounds"}
                seq_keys = {"lookback","hidden","hidden_dim","num_layers","dropout","pooling","bidirectional","use_attention","layer_norm","batch_size","epochs","patience","lr","weight_decay","device","d_model","nhead","dim_feedforward","alpha_init"}
                def fam(d: dict) -> str:
                    ks = set(d.keys())
                    if ks & xgb_keys: return 'xgb'
                    if ks & seq_keys: return 'seq'
                    return 'other'
                if str(model).lower() in {"xgb","xgboost"}:
                    choices = [c for c in choices if isinstance(c, dict) and fam(c) == 'xgb'] or [choices[0]]
                else:
                    choices = [c for c in choices if isinstance(c, dict) and fam(c) == 'seq'] or [choices[0]]
                space["model_params"] = {"type": "categorical", "choices": choices}
            logger.info("model_rotation_applied", round=round_idx, model=str(model))
    except Exception as e:
        logger.warning("model_rotation_apply_failed", error=str(e))

    # Save degraded config
    with open(output_path, "w") as f:
        json.dump(cfg, f, indent=2)

    logger.info(
        "config_degradation_applied",
        input=config_path,
        output=output_path,
        level=degrade_cfg["level"],
        max_points=cfg["guard"]["max_total_points"],
        round=round_idx
    )
