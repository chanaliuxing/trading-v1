#!/usr/bin/env python3
from __future__ import annotations
"""
AI Trader - Control Event Consumer

Listens on the configured control topic and reacts to model/OOT flows, e.g.:
- type: "oof_complete" → notify Brain to refresh plan (afterhours-style) immediately

Best-effort semantics:
- If Kafka unavailable, exits quietly.
- Uses a per-day lock to avoid spamming Brain on bursty events (TTL 15 min).
"""
import sys
from pathlib import Path

# Ensure project root is on sys.path so that "libs" and "scripts.*" imports work
_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _THIS_FILE.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Set, Optional

from libs.logger import get_logger
from libs.config import config as _cfg

# Retry with exponential backoff
try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    TENACITY_AVAILABLE = True
except ImportError:
    TENACITY_AVAILABLE = False
    logger = get_logger("ai_trader.control_consumer")
    logger.warning("tenacity_not_available", msg="Install tenacity for retry support")

# Prometheus metrics (optional)
try:
    from prometheus_client import Counter, Histogram
    events_processed = Counter('control_events_processed_total', 'Events processed', ['event_type', 'status'])
    event_processing_duration = Histogram('control_event_duration_seconds', 'Event processing duration', ['event_type'])
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False

logger = get_logger("ai_trader.control_consumer")

_orchestrator_instance: Optional[Any] = None

# Training-only mode: when running via tools/ml_brain_loop (ML_BRAIN_LOOP=1),
# we relax some cooldowns/thresholds to allow more aggressive experimentation
TRAINING_ONLY = os.getenv("ML_BRAIN_LOOP", "").strip().lower() in {"1", "true", "yes", "on"}


def _get_model_confidence_multiplier(
    action: Dict[str, Any],
    symbol: str,
    model_family: str = "",
    horizon: str = ""
) -> float:
    """
    根据模型真实战绩（从 ClickHouse）计算 confidence 倍数。

    使用 ClickHouse 中的 trading.ml_eval_with_delta 表查询模型表现指标：
    - latest_sharpe: Sharpe 比率
    - success_rate_30d: 最近 30 天成功率
    - fail_streak: 连续失败次数

    Returns:
        float: confidence multiplier (0.0 ~ 2.0)
               - 0.0: 模型太烂，禁用
               - 0.5-0.7: 模型表现差，降权
               - 1.0: 模型正常
               - 1.2-1.5: 模型表现优异，加权
    """
    try:
        from scripts.ai_trader.brain.policy.memory import get_model_stats_summary_from_ch
    except ImportError:
        # Fallback: if function not available, use neutral weight
        logger.warning(
            "model_stats_function_unavailable",
            msg="get_model_stats_summary_from_ch not found, using neutral multiplier"
        )
        return 1.0

    # 获取模型统计
    try:
        stats = get_model_stats_summary_from_ch(
            symbol=symbol,
            model_family=model_family,
            horizon=horizon,
            window_days=0  # 使用默认 30 天
        )
    except Exception as e:
        logger.error(
            "model_stats_query_failed",
            symbol=symbol,
            model_family=model_family,
            error=str(e),
            exc_info=True
        )
        # 查询失败时使用中性权重
        return 1.0

    # 提取指标
    sharpe = stats.get("latest_sharpe", 0.0)
    success_rate = stats.get("success_rate_30d", 0.5)
    fail_streak = stats.get("fail_streak", 0)
    attempts = stats.get("attempts_30d", 0)

    # 硬门槛：直接禁用
    if fail_streak >= 5:
        logger.warning(
            "model_disabled_fail_streak",
            symbol=symbol,
            model_family=model_family,
            horizon=horizon,
            fail_streak=fail_streak,
            reason="连续失败次数过多"
        )
        return 0.0

    if attempts >= 10 and sharpe < -0.5:
        logger.warning(
            "model_disabled_negative_sharpe",
            symbol=symbol,
            model_family=model_family,
            horizon=horizon,
            sharpe=sharpe,
            attempts=attempts,
            reason="Sharpe 严重负值"
        )
        return 0.0

    if attempts >= 10 and success_rate < 0.3:
        logger.warning(
            "model_disabled_low_success_rate",
            symbol=symbol,
            model_family=model_family,
            horizon=horizon,
            success_rate=success_rate,
            attempts=attempts,
            reason="成功率过低"
        )
        return 0.0

    # 软门槛：调整权重
    if attempts < 5:
        # 样本不足，使用中性权重
        logger.debug(
            "model_insufficient_attempts",
            symbol=symbol,
            model_family=model_family,
            attempts=attempts,
            multiplier=1.0
        )
        return 1.0

    # 基于 Sharpe 的权重调整
    if sharpe >= 1.5:
        multiplier = 1.5  # 优秀模型，加权
    elif sharpe >= 1.0:
        multiplier = 1.2  # 良好模型，略微加权
    elif sharpe >= 0.5:
        multiplier = 1.0  # 正常模型
    elif sharpe >= 0.0:
        multiplier = 0.7  # 平庸模型，降权
    else:
        multiplier = 0.5  # 表现较差，大幅降权

    # 基于成功率的微调
    if success_rate > 0.6:
        multiplier *= 1.1  # 成功率高，额外加成
    elif success_rate < 0.4:
        multiplier *= 0.9  # 成功率低，额外惩罚

    logger.info(
        "model_confidence_calibrated",
        symbol=symbol,
        model_family=model_family,
        horizon=horizon,
        sharpe=round(sharpe, 3),
        success_rate=round(success_rate, 3),
        fail_streak=fail_streak,
        attempts=attempts,
        multiplier=round(multiplier, 3)
    )

    return multiplier


def _runs_dir() -> Path:
    try:
        from scripts.ai_trader.orchestrator import Orchestrator  # lazy import
        o = Orchestrator()
        return Path(o.runs_dir)
    except Exception:
        return Path("runs/ai_trader")


def _append_event_line(evt: Dict[str, Any]) -> None:
    dstr = date.today().isoformat()
    out = _runs_dir() / dstr / "ai_events.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _should_rate_limit(tag: str, ttl_sec: int = 15 * 60) -> bool:
    dstr = date.today().isoformat()
    p = _runs_dir() / dstr / "locks" / f"{tag}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    try:
        if p.exists():
            age = now - int(p.stat().st_mtime)
            if age < ttl_sec:
                return True
        p.write_text(str(now), encoding="utf-8")
    except Exception:
        # If lock write fails, do not rate-limit
        return False
    return False


def _send_to_dlq(evt: Dict[str, Any], error: str) -> None:
    """Write failed event to Dead Letter Queue for manual inspection."""
    dstr = date.today().isoformat()
    dlq = _runs_dir() / dstr / "dlq_control.jsonl"
    dlq.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dlq.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "event": evt,
                "error": error,
                "retries_exhausted": True
            }, ensure_ascii=False) + "\n")
        logger.warning("event_sent_to_dlq", event_type=evt.get("type"), error=error)
    except Exception as e:
        logger.error("dlq_write_failed", err=str(e), exc_info=True)


# --- Optimization state helpers (must be defined before handlers use them) ---
def _opt_state_file() -> Path:
    return _runs_dir() / date.today().isoformat() / "ml_optimization_state.json"


def _load_opt_state() -> Dict[str, Any]:
    try:
        fp = _opt_state_file()
        if fp.exists():
            return json.loads(fp.read_text("utf-8"))
    except Exception:
        pass
    return {"fail_streak": 0, "attempts": {"data": 0, "params": 0, "hpo": 0}, "last_update": datetime.now().isoformat()}


def _save_opt_state(state: Dict[str, Any]) -> None:
    try:
        fp = _opt_state_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        state["last_update"] = datetime.now().isoformat()
        fp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _should_trigger_evolver(state: Dict[str, Any]) -> bool:
    """Read evolution_policy.yaml to decide if evolver can run."""
    try:
        import yaml

        policy_path = Path("config/evolution_policy.yaml")
        if not policy_path.exists():
            return False
        cfg = yaml.safe_load(policy_path.read_text()) or {}
        min_fail_streak = int(cfg.get("min_fail_streak", 3))
        max_runs = int(cfg.get("max_runs_per_day", 0))
        fs = int(state.get("fail_streak", 0))
        # Count runs today
        runs_dir = _runs_dir()
        count = 0
        for p in runs_dir.glob("evolver_*.json"):
            try:
                meta = json.loads(p.read_text())
                ts = meta.get("ts") or meta.get("timestamp")
                if ts and ts.startswith(date.today().isoformat()):
                    count += 1
            except Exception:
                continue
        return fs >= min_fail_streak and (max_runs <= 0 or count < max_runs)
    except Exception:
        return False


def _eligible_to_pause(state: Dict[str, Any]) -> bool:
    # Global switch: allow_pause=false → never pause (keep optimizing)
    try:
        allow_pause = bool(((_cfg.get("policies") or {}).get("ml") or {}).get("allow_pause", False))
        if not allow_pause:
            return False
    except Exception:
        return False
    try:
        fs = int(state.get("fail_streak", 0))
        at = state.get("attempts", {}) or {}
        distinct = sum(1 for k in ("data", "params", "hpo") if int(at.get(k, 0)) > 0)
        return fs >= 3 and distinct >= 3
    except Exception:
        return False


def _bump_attempt(state: Dict[str, Any], category: str) -> None:
    at = state.setdefault("attempts", {})
    try:
        at[category] = int(at.get(category, 0)) + 1
    except Exception:
        at[category] = 1


def _translate_brain_actions(actions: list[dict]) -> list[dict]:
    out: list[dict] = []
    for a in actions or []:
        try:
            t = (a.get("action") or "").lower()
            p = a.get("params", {}) or {}
            c = float(a.get("confidence", 0.0) or 0.0)
            # map unsupported verbs to platform verbs
            if t in {"stop"}:
                t = "pause_ml"
            elif t in {"run_hpo", "run_alpha", "hpo"}:
                # Normalize to scheduler queue_job with nested params
                trials = int(p.get("n_trials", 30)) if isinstance(p, dict) else 30
                timeout = float(p.get("timeout_hours", 1.0)) if isinstance(p, dict) else 1.0
                syms = p.get("symbols", []) if isinstance(p, dict) else []
                t = "queue_job"; p = {"job_type": "alpha_loop", "params": {"n_trials": trials, "timeout_hours": timeout, "symbols": syms}}
            elif t in {"adjust_thresholds"}:
                # safer: extend window rather than relaxing quality gates
                t = "extend_window"; p = {"window_days": min(max(int(p.get("window_days", 30)), 21), 60)}
            elif t in {"switch_provider"}:
                t = "queue_job"; p = {"job_type": "alpha_loop", "params": {}}

            # Normalize fusion weights fix_config to ensemble_sweep job
            if t == "fix_config":
                path = str(p.get("path") or "").lower()
                key = str(p.get("key") or "").lower()
                val = p.get("value")
                if ("model_fusion_weights" in path) or ("model_fusion_weights" in key) or (isinstance(val, dict) and set(val.keys()) & {"xgb", "lstm"}):
                    try:
                        center = {"xgb": float(val.get("xgb", 0.5)), "lstm": float(val.get("lstm", 0.5))} if isinstance(val, dict) else {"xgb": 0.5, "lstm": 0.5}
                    except Exception:
                        center = {"xgb": 0.5, "lstm": 0.5}
                    base = center.get("xgb", 0.5)
                    grid = [max(0.0, min(0.9, round(base + d, 2))) for d in (-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3)]
                    t = "queue_job"
                    p = {"job_type": "ensemble_sweep", "params": {"grid": sorted(set(grid))}}
            # Feature management: propose_features → write catalog and enqueue build
            if t == "propose_features":
                # Normalize into a special marker action; execution handled later
                p = {"version": str(p.get("version") or "auto"), "specs": p.get("specs") or []}
            # Final normalization for queue_job shape and key mapping
            if t == "queue_job":
                job_type = (p.get("job_type") if isinstance(p, dict) else None) or ""
                job_params = (p.get("params") if isinstance(p, dict) else None)
                # If Brain put params at top-level, move them under 'params'
                if job_params is None or not isinstance(job_params, dict):
                    job_params = {}
                    # lift common keys if present at top-level
                    for key in ("symbols", "n_trials", "timeout_hours", "days", "samples"):
                        if isinstance(p, dict) and key in p:
                            job_params[key] = p[key]
                    p = {"job_type": job_type or p.get("job_type", "alpha_loop"), "params": job_params}
                else:
                    # ensure nested params contain lifted keys if also present at top-level (defensive)
                    for key in ("symbols", "n_trials", "timeout_hours", "days", "samples"):
                        if key in p and key not in job_params:
                            job_params[key] = p[key]
                    p = {"job_type": job_type or p.get("job_type", "alpha_loop"), "params": job_params}

            out.append({"action": t, "params": p, "confidence": c})
        except Exception:
            continue
    return out


# --- Success gates & auto-apply helpers ---
def _load_success_gates() -> Dict[str, float]:
    """Load success gates from config.yml: policies.ml.success_gates

    Keys supported (all optional): sharpe_min, profit_factor_min, total_return_min, max_drawdown_max, win_rate_min
    """
    try:
        pol = (_cfg.get("policies") or {}).get("ml") or {}
        gates = pol.get("success_gates") or {}
        out = {}
        for k in ["sharpe_min", "profit_factor_min", "total_return_min", "max_drawdown_max", "win_rate_min"]:
            if k in gates and gates[k] is not None:
                out[k] = float(gates[k])
        return out
    except Exception:
        return {}


def _load_feature_success_gates() -> Dict[str, Any]:
    try:
        pol = (_cfg.get("policies") or {}).get("features") or {}
        gates = pol.get("success_gates") or {}
        return {
            "auc_lift_min": float(gates.get("auc_lift_min", 0.02) or 0.02),
            "ic_lift_min": float(gates.get("ic_lift_min", 0.01) or 0.01),
            "coverage_min": float(gates.get("coverage_min", 0.98) or 0.98),
            "leakage_forbidden": bool(gates.get("leakage_forbidden", True)),
            "max_inter_feature_corr": float(gates.get("max_inter_feature_corr", 0.95) or 0.95),
        }
    except Exception:
        return {"auc_lift_min": 0.02, "ic_lift_min": 0.01, "coverage_min": 0.98, "leakage_forbidden": True, "max_inter_feature_corr": 0.95}


def _load_hpo_success_gates() -> Dict[str, Any]:
    """Load HPO success gates from config.yml: policies.hpo.success_gates

    Returns dict with keys: min_trials, min_unique_params, convergence_score_max,
                            min_ic (optional), min_coverage (optional)
    """
    try:
        pol = (_cfg.get("policies") or {}).get("hpo") or {}
        gates = pol.get("success_gates") or {}
        result = {
            "min_trials": int(gates.get("min_trials", 8) or 8),
            "min_unique_params": int(gates.get("min_unique_params", 6) or 6),
            "convergence_score_max": float(gates.get("convergence_score_max", 0.30) or 0.30),
        }

        # NEW: Optional L1 gates (Model Layer metrics)
        if "min_ic" in gates and gates["min_ic"] is not None:
            result["min_ic"] = float(gates["min_ic"])
        if "min_coverage" in gates and gates["min_coverage"] is not None:
            result["min_coverage"] = float(gates["min_coverage"])

        return result
    except Exception:
        return {"min_trials": 8, "min_unique_params": 6, "convergence_score_max": 0.30}


def _metrics_pass_gates(m: Dict[str, float], gates: Dict[str, float]) -> tuple[bool, Dict[str, bool]]:
    checks: Dict[str, bool] = {}
    if not gates:
        return False, checks
    def _get(name: str) -> float:
        try:
            return float(m.get(name, 0.0))
        except Exception:
            return 0.0
    ok = True
    if "sharpe_min" in gates:
        checks["sharpe_min"] = _get("sharpe") >= gates["sharpe_min"]
        ok = ok and checks["sharpe_min"]
    if "profit_factor_min" in gates:
        checks["profit_factor_min"] = _get("profit_factor") >= gates["profit_factor_min"]
        ok = ok and checks["profit_factor_min"]
    if "total_return_min" in gates:
        checks["total_return_min"] = _get("total_return") >= gates["total_return_min"]
        ok = ok and checks["total_return_min"]
    if "max_drawdown_max" in gates:
        # drawdown is negative value or positive? Assume positive magnitude here
        checks["max_drawdown_max"] = _get("max_drawdown") <= gates["max_drawdown_max"]
        ok = ok and checks["max_drawdown_max"]
    if "win_rate_min" in gates:
        checks["win_rate_min"] = _get("win_rate") >= gates["win_rate_min"]
        ok = ok and checks["win_rate_min"]
    return ok, checks


def _append_success_audit(entry: Dict[str, Any]) -> None:
    try:
        d = _runs_dir() / date.today().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "ml_success.jsonl"
        with fp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _apply_ensemble_on_success(compare_json: Path, symbols: list[str]) -> Dict[str, Any]:
    """Update ensemble policy from compare.json and try applying weights.

    Returns dict with status and command traces.
    """
    res = {"status": "ok", "steps": []}
    try:
        # Step 1: choose_ensemble --update-policy
        cmd1 = [sys.executable, "tools/choose_ensemble.py", "--compare", str(compare_json), "--policy", "config/ensemble_policies.json", "--update-policy"]
        p1 = subprocess.run(cmd1, text=True, capture_output=True)
        res["steps"].append({"cmd": " ".join(cmd1), "rc": p1.returncode, "stderr_tail": p1.stderr[-200:]})
        # Step 2: apply_ensemble_policy (respect autosettings; pass --force if require_approval=false)
        try:
            autos = ((_cfg.get("policies") or {}).get("autosettings") or {})
            force = (autos.get("enabled") and not autos.get("require_approval", True))
        except Exception:
            force = False
        cmd2 = [sys.executable, "tools/apply_ensemble_policy.py", "--policy", "config/ensemble_policies.json"]
        if symbols:
            cmd2 += ["--symbols", ",".join(symbols)]
        if force:
            cmd2.append("--force")
        p2 = subprocess.run(cmd2, text=True, capture_output=True)
        res["steps"].append({"cmd": " ".join(cmd2), "rc": p2.returncode, "stderr_tail": p2.stderr[-200:]})
    except Exception as e:
        res["status"] = "error"
        res["error"] = str(e)
    return res


async def _run_ml_diagnostics_and_execute(evt: Dict[str, Any]) -> None:
    """
    Run ML diagnostics after OOF/hpo_cli completion and execute recommended actions.

    Extracts ML health metrics from event, calls ml_diagnostics.review_ml_health(),
    and enqueues recommended jobs (feature_build, hpo_cli, alpha_loop) with cooldown.

    P2.3: ML Diagnostics Integration
    """
    job_id = evt.get("job_id")
    job_type = evt.get("job_type")
    # Work on a shallow copy so we can normalize metrics safely
    raw_metrics = evt.get("metrics", {}) or {}
    metrics = dict(raw_metrics)

    # Skip if not ML training job
    if job_type not in ["oof", "hpo_cli"]:
        return

    # Normalize OOF metrics into a common schema so diagnostics/Brain can reason about them.
    # For nightly_oof_flow, we get fusion_* keys; map them to generic ic/auc/coverage when missing.
    try:
        if job_type == "oof":
            if "fusion_ic" in metrics and "ic" not in metrics:
                try:
                    metrics["ic"] = float(metrics["fusion_ic"])
                except Exception:
                    pass
            if "fusion_auc" in metrics and "auc" not in metrics:
                try:
                    metrics["auc"] = float(metrics["fusion_auc"])
                except Exception:
                    pass
            # Approximate coverage as fraction of bars with valid predictions if available
            if "coverage" not in metrics:
                try:
                    n_bars = float(metrics.get("n_bars", 0.0) or 0.0)
                    fusion_n = float(metrics.get("fusion_n", 0.0) or 0.0)
                    if n_bars > 0:
                        metrics["coverage"] = max(0.0, min(1.0, fusion_n / n_bars))
                except Exception:
                    pass
    except Exception:
        # Never let normalization break diagnostics
        pass

    # Check cooldown to prevent spam (relaxed in training-only mode)
    if (not TRAINING_ONLY) and _is_success_cooldown_active("ml_diagnostics"):
        logger.info("ml_diagnostics_cooldown_active", job_id=job_id, job_type=job_type)
        return

    try:
        # Import diagnostics module
        from scripts.ai_trader.brain.ml_diagnostics import review_ml_health

        # Extract ML health metrics
        ml_health = {
            "model_layer": {
                "ic": float(metrics.get("ic", 0.0) or 0.0),
                "ir": float(metrics.get("ir", 0.0) or 0.0),
                "coverage": float(metrics.get("coverage", 0.0) or 0.0),
                "status": "healthy" if float(metrics.get("ic", 0.0) or 0.0) >= 0.20 else "degraded"
            },
            "strategy_layer": {
                "sharpe": float(metrics.get("sharpe", 0.0) or 0.0),
                "mdd": float(metrics.get("mdd", 0.0) or metrics.get("max_drawdown", 0.0) or 0.0),
                "status": "healthy" if float(metrics.get("sharpe", 0.0) or 0.0) >= 1.0 else "suboptimal"
            },
            "ready_for_trading": (
                float(metrics.get("ic", 0.0) or 0.0) >= 0.20 and
                float(metrics.get("sharpe", 0.0) or 0.0) >= 1.0
            )
        }

        # Run diagnostics
        diagnosis = review_ml_health(ml_health)

        # Log diagnosis
        logger.info(
            "ml_diagnostics_result",
            job_id=job_id,
            job_type=job_type,
            diagnosis=diagnosis["diagnosis"],
            severity=diagnosis["severity"],
            summary=diagnosis["summary"],
            actions_count=len(diagnosis["next_actions"])
        )

        # Save diagnosis to audit log
        d = _runs_dir() / date.today().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        diag_path = d / f"ml_diagnosis_{job_id}.json"
        with diag_path.open("w", encoding="utf-8") as f:
            json.dump({
                "ts": datetime.now().isoformat(),
                "job_id": job_id,
                "job_type": job_type,
                "ml_health": ml_health,
                "diagnosis": diagnosis
            }, f, ensure_ascii=False, indent=2)

        # Execute top priority action (if severity is critical/warning)
        if diagnosis["severity"] in ["critical", "warning"] and diagnosis["next_actions"]:
            top_action = diagnosis["next_actions"][0]  # Already sorted by priority
            action_type = top_action["action"]

            # Map action to job_type
            job_type_map = {
                "feature_build": "feature_build",
                "run_hpo_cli": "hpo_cli",
                "alpha_loop": "alpha_loop",
                "change_prediction_task": None,  # Manual intervention required
                "adjust_risk_controls": None,    # Manual intervention required
                "investigate_coverage": None,    # Manual investigation
                "monitor": None,                 # No action needed
                "incremental_improve": "hpo_cli", # Default to HPO
                "bulk_backfill": "bulk_backfill", # Long range backfill via scheduler
            }

            enqueue_job_type = job_type_map.get(action_type)

            if enqueue_job_type:
                # Enqueue recommended job
                try:
                    enqueue_params = {
                        "job_type": enqueue_job_type,
                        "priority": 50 + (10 if top_action["priority"] == 1 else 0),  # 60 for high priority
                        "params": top_action.get("params", {})
                    }

                    result = await _enqueue_ml_job(enqueue_params, job_id)

                    logger.info(
                        "ml_diagnostics_action_executed",
                        job_id=job_id,
                        action=action_type,
                        enqueued_job_type=enqueue_job_type,
                        queued_job_id=result.get("queued_job_id"),
                        reasoning=top_action["reasoning"]
                    )

                    # Set cooldown after executing action (6 hours)
                    _set_success_cooldown("ml_diagnostics", ttl_sec=6*3600)

                except Exception as e:
                    logger.error("ml_diagnostics_action_failed", job_id=job_id, action=action_type, error=str(e), exc_info=True)
            else:
                logger.info(
                    "ml_diagnostics_action_manual",
                    job_id=job_id,
                    action=action_type,
                    reasoning=top_action["reasoning"],
                    msg="Action requires manual intervention"
                )

    except Exception as e:
        logger.error("ml_diagnostics_failed", job_id=job_id, error=str(e), exc_info=True)


def _save_last_hpo_best_params(best_params: Dict[str, Any]) -> None:
    """Persist last accepted HPO best_params so Brain can inspect model hyperparameters.

    This is intentionally generic: the structure is not interpreted here; the
    full JSON payload is saved and later exposed to the ML Brain context as
    hpo_last_best_params for downstream analysis (e.g., detecting XGBoost
    deadlocks such as tiny learning_rate or oversized min_child_weight).
    """
    try:
        if not isinstance(best_params, dict) or not best_params:
            return
        d = _runs_dir() / date.today().isoformat() / "cache"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "hpo_last_best_params.json"
        fp.write_text(json.dumps(best_params, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("hpo_last_best_params_saved", file=str(fp))
    except Exception as exc:
        logger.warning("hpo_last_best_params_save_failed", error=str(exc))


async def _evaluate_and_apply_on_success(evt: Dict[str, Any]) -> None:
    """Apply job-specific success handling.

    - OOF: check backtest gates then apply ensemble
    - ensemble_sweep: apply best_weights if present (no gates)
    - alpha_loop: apply HPO params (no backtest gates)
    - feature_eval: feature gates handled separately below
    """
    job_id = evt.get("job_id")
    job_type = evt.get("job_type")
    metrics = evt.get("metrics", {}) or {}
    gates = _load_success_gates()  # used for OOF only

    comp_metrics = {}
    symbols = []
    compare_json = None
    if job_type == "oof":
        bt = metrics.get("bt") or {}
        best_mode = metrics.get("best_mode")
        if best_mode and isinstance(bt.get(best_mode), dict):
            comp_metrics = bt[best_mode]
        arts = evt.get("artifacts") or []
        for a in arts:
            if str(a).endswith("compare.json"):
                compare_json = Path(str(a))
                break
        try:
            if compare_json and compare_json.exists():
                data = json.loads(compare_json.read_text("utf-8"))
                params = data.get("params") or {}
                symbols = [str(s).upper() for s in (params.get("symbols") or [])]
        except Exception:
            pass

        ok, checks = _metrics_pass_gates(comp_metrics, gates)
        _append_success_audit({
            "ts": datetime.now().isoformat(),
            "job_id": job_id,
            "job_type": job_type,
            "metrics": comp_metrics,
            "gates": gates,
            "checks": checks,
            "pass": ok
        })
        if not ok:
            return

    # On success: for OOF, update/apply ensemble weights
    if job_type == "oof" and compare_json and compare_json.exists():
        # Non-regression guard: compare against current champion metrics if available
        try:
            reg_path = Path("scripts/ai_trader/registry/champion_challenger.json")
            champion_ok = True
            reason = ""
            if reg_path.exists():
                reg = json.loads(reg_path.read_text("utf-8"))
                ch = reg.get("champion") or {}
                # Extract comparables
                new_pf = float((comp_metrics or {}).get("profit_factor", 0.0) or 0.0)
                new_sharpe = float((comp_metrics or {}).get("sharpe", 0.0) or 0.0)
                new_ret = float((comp_metrics or {}).get("total_return", 0.0) or 0.0)
                ch_pf = float(ch.get("profit_factor", 0.0) or 0.0)
                ch_sharpe = float(ch.get("sharpe", 0.0) or 0.0)
                ch_ret = float(ch.get("total_return", 0.0) or 0.0)
                # Configurable tolerance (defaults: no degradation allowed)
                tol = (((_cfg.get("policies") or {}).get("apply") or {}).get("non_regression_tolerance") or {})
                pf_drop = float(tol.get("profit_factor_max_drop", 0.0) or 0.0)
                sharpe_drop = float(tol.get("sharpe_max_drop", 0.0) or 0.0)
                ret_drop = float(tol.get("total_return_max_drop", 0.0) or 0.0)
                # Allow apply only if not worse than champion beyond tolerance on all key metrics
                pf_ok = (new_pf + 1e-9) >= (ch_pf - pf_drop)
                sharpe_ok = (new_sharpe + 1e-9) >= (ch_sharpe - sharpe_drop)
                ret_ok = (new_ret + 1e-9) >= (ch_ret - ret_drop)
                champion_ok = pf_ok and sharpe_ok and ret_ok
                if not champion_ok:
                    reason = f"non_regression_guard: new_pf={new_pf} ch_pf={ch_pf} tol={pf_drop}; new_sharpe={new_sharpe} ch_sharpe={ch_sharpe} tol={sharpe_drop}; new_ret={new_ret} ch_ret={ch_ret} tol={ret_drop}"
            if not champion_ok:
                logger.warning("apply_blocked_by_non_regression", job_id=job_id, reason=reason)
                return
        except Exception as e:
            logger.warning("non_regression_guard_error", job_id=job_id, error=str(e))
        res = _apply_ensemble_on_success(compare_json, symbols)
        logger.info("success_apply_ensemble", job_id=job_id, status=res.get("status"))
    elif job_type == "ensemble_sweep":
        # Apply best weights from ensemble sweep
        try:
            best_w = {}
            try:
                best_w = metrics.get("best_weights") or {}
            except Exception:
                best_w = {}
            if best_w:
                # Update policy file and apply
                from scripts.ai_trader.config import json_policy_patcher
                patch = json_policy_patcher.update("config/ensemble_policies.json", "defaults.weights", best_w)
                if patch.get("ok"):
                    # Apply policy to system (respect autosettings)
                    import sys as _sys, subprocess as _sp
                    cmd = [_sys.executable, "tools/apply_ensemble_policy.py", "--policy", "config/ensemble_policies.json"]
                    p = _sp.run(cmd, text=True, capture_output=True)
                    logger.info("ensemble_sweep_applied", job_id=job_id, weights=best_w, rc=p.returncode)
                else:
                    logger.warning("ensemble_policy_patch_failed", job_id=job_id, error=patch.get("error"))
        except Exception as e:
            logger.error("ensemble_sweep_apply_failed", job_id=job_id, error=str(e), exc_info=True)
    elif job_type == "alpha_loop" or job_type == "hpo_cli":
        # Apply HPO params after checking success gates
        try:
            # Defensive check for HPO metrics
            if not ("trials" in metrics or "unique_params" in metrics or "best_params" in metrics):
                logger.warning("alpha_loop_missing_hpo_metrics", job_id=job_id, job_type=job_type, metrics=metrics)
                return

            hpo_gates = _load_hpo_success_gates()
            hpo_metrics = metrics  # metrics from alpha_loop contains trials/unique_params/convergence_score

            # Extract HPO-specific metrics
            trials = int(hpo_metrics.get("trials", 0) or 0)
            unique_params = int(hpo_metrics.get("unique_params", 0) or 0)
            convergence_score = float(hpo_metrics.get("convergence_score", 1.0) or 1.0)

            # NEW: Extract L1 metrics from best_params (Model Layer)
            best_params = hpo_metrics.get("best_params") or {}
            ic = float(best_params.get("ic", 0.0) or 0.0)
            coverage = float(best_params.get("coverage", 0.0) or 0.0)

            # Check gates
            checks = {
                "min_trials": trials >= hpo_gates["min_trials"],
                "min_unique_params": unique_params >= hpo_gates["min_unique_params"],
                "convergence_score_max": convergence_score <= hpo_gates["convergence_score_max"]
            }

            # NEW: Check L1 gates if present
            if "min_ic" in hpo_gates:
                checks["min_ic"] = ic >= hpo_gates["min_ic"]
            if "min_coverage" in hpo_gates:
                checks["min_coverage"] = coverage >= hpo_gates["min_coverage"]

            passed = all(checks.values())

            # Audit gate check
            audit_entry = {
                "ts": datetime.now().isoformat(),
                "job_id": job_id,
                "job_type": job_type,
                "metrics": {
                    "trials": trials,
                    "unique_params": unique_params,
                    "convergence_score": convergence_score,
                    "ic": ic,  # NEW
                    "coverage": coverage  # NEW
                },
                "gates": hpo_gates,
                "checks": checks,
                "pass": passed
            }

            # NEW: Add multi-objective info if present
            if "selection_rule" in best_params:
                audit_entry["selection_rule"] = best_params["selection_rule"]
            if "pareto_size" in best_params:
                audit_entry["pareto_size"] = best_params["pareto_size"]

            _append_success_audit(audit_entry)

            if passed:
                # Apply params if gates passed. For hpo_cli, prefer best_params in metrics; otherwise fall back to artifacts.
                try:
                    if job_type == "hpo_cli":
                        # best_params already extracted above for L1 gate checking
                        if best_params:
                            # Persist last accepted best_params snapshot for Brain
                            _save_last_hpo_best_params(best_params)
                            # Write a temp file and call apply_extras_params.py directly
                            tmpf = _runs_dir() / date.today().isoformat() / f"hpo_cli_{job_id}_best_params.json"
                            tmpf.parent.mkdir(parents=True, exist_ok=True)
                            tmpf.write_text(json.dumps(best_params, ensure_ascii=False, indent=2), encoding="utf-8")
                            import sys as _sys, subprocess as _sp
                            cmd = [_sys.executable, "apply_extras_params.py", "--hpo-params", str(tmpf), "--config-yaml", "config.yml", "--strategy-id", "enhanced_ml_001"]
                            _sp.run(cmd, text=True, capture_output=True)
                        else:
                            _apply_hpo_params_from_artifacts(evt)
                    else:
                        _apply_hpo_params_from_artifacts(evt)
                except Exception:
                    _apply_hpo_params_from_artifacts(evt)
                logger.info("hpo_params_applied", job_id=job_id, job_type=job_type, trials=trials, unique_params=unique_params, convergence_score=convergence_score)
            else:
                logger.warning("alpha_loop_gates_failed", job_id=job_id, metrics=hpo_metrics, gates=hpo_gates, checks=checks)
        except Exception as e:
            logger.warning("alpha_loop_params_apply_failed", job_id=job_id, error=str(e))
    elif job_type == "feature_eval":
        # Evaluate feature success gates and enqueue apply if passed
        try:
            gates_f = _load_feature_success_gates()
            auc_ok = float(metrics.get("auc_lift", 0.0) or 0.0) >= gates_f["auc_lift_min"]
            ic_ok = float(metrics.get("ic_lift", 0.0) or 0.0) >= gates_f["ic_lift_min"]
            cov_ok = float(metrics.get("coverage", 0.0) or 0.0) >= gates_f["coverage_min"]
            leak_ok = (not bool(metrics.get("leakage", False))) if gates_f.get("leakage_forbidden", True) else True
            corr_ok = True
            try:
                mic = float(metrics.get("max_inter_feature_corr", 0.0) or 0.0)
                corr_ok = (mic <= float(gates_f.get("max_inter_feature_corr", 0.95)))
            except Exception:
                corr_ok = True
            if auc_ok and ic_ok and cov_ok and leak_ok and corr_ok:
                # Promote candidates to active
                try:
                    # Reuse enqueue to apply
                    _ = asyncio.get_event_loop().create_task(_enqueue_ml_job({"job_type": "feature_apply", "params": {}}, job_id))
                except Exception:
                    pass
                logger.info("features_success_gate_pass", job_id=job_id, metrics=metrics, gates=gates_f)
            else:
                logger.info("features_success_gate_fail", job_id=job_id, metrics=metrics, gates=gates_f)
        except Exception as e:
            logger.warning("features_success_gate_eval_error", job_id=job_id, error=str(e))
    elif job_type == "feature_build":
        # Chain: trigger feature_eval after successful build
        try:
            _ = asyncio.get_event_loop().create_task(_enqueue_ml_job({"job_type": "feature_eval", "params": {}}, job_id))
            logger.info("feature_build_enqueued_eval", job_id=job_id)
        except Exception:
            pass
    elif job_type == "feature_ablation":
        # Apply ablation suggestions by mutating catalog and trigger a rebuild
        try:
            rep = _runs_dir() / date.today().isoformat() / "feature_ablation" / "ablation_report.json"
            if rep.exists():
                data = json.loads(rep.read_text("utf-8"))
                remove_list = [str(x) for x in (data.get("suggest_remove") or []) if x]
                if remove_list:
                    catp = Path("config/features_catalog.json")
                    try:
                        cat = json.loads(catp.read_text("utf-8")) if catp.exists() else {"version": date.today().isoformat(), "active": [], "candidate": []}
                    except Exception:
                        cat = {"version": date.today().isoformat(), "active": [], "candidate": []}
                    cand = [c for c in (cat.get("candidate") or []) if isinstance(c, dict) and c.get("name") not in remove_list]
                    cat["candidate"] = cand
                    cat["version"] = f"{str(cat.get('version') or date.today().isoformat())}-ab_apply"
                    catp.write_text(json.dumps(cat, indent=2, ensure_ascii=False), encoding="utf-8")
                    _ = asyncio.get_event_loop().create_task(_enqueue_ml_job({"job_type": "feature_build", "params": {"catalog": str(catp)}}, job_id))
                    logger.info("feature_ablation_applied_and_build_enqueued", job_id=job_id, removed=remove_list)
                else:
                    logger.info("feature_ablation_no_suggestions", job_id=job_id)
            else:
                logger.warning("feature_ablation_report_missing", job_id=job_id, path=str(rep))
        except Exception as e:
            logger.warning("feature_ablation_apply_failed", job_id=job_id, error=str(e))
    elif job_type in {"alpha_loop", "hpo_cli"}:
        # Optional non-regression guard for HPO applies only if backtest metrics present (rare)
        try:
            reg_path = Path("scripts/ai_trader/registry/champion_challenger.json")
            if reg_path.exists():
                ch = json.loads(reg_path.read_text("utf-8")).get("champion") or {}
                new_pf = float((metrics or {}).get("profit_factor", 0.0) or 0.0)
                new_sharpe = float((metrics or {}).get("sharpe", 0.0) or 0.0)
                new_ret = float((metrics or {}).get("total_return", 0.0) or 0.0)
                ch_pf = float(ch.get("profit_factor", 0.0) or 0.0)
                ch_sharpe = float(ch.get("sharpe", 0.0) or 0.0)
                ch_ret = float(ch.get("total_return", 0.0) or 0.0)
                tol = (((_cfg.get("policies") or {}).get("apply") or {}).get("non_regression_tolerance") or {})
                pf_ok = (new_pf + 1e-9) >= (ch_pf - float(tol.get("profit_factor_max_drop", 0.0) or 0.0))
                sharpe_ok = (new_sharpe + 1e-9) >= (ch_sharpe - float(tol.get("sharpe_max_drop", 0.0) or 0.0))
                ret_ok = (new_ret + 1e-9) >= (ch_ret - float(tol.get("total_return_max_drop", 0.0) or 0.0))
                if not (pf_ok and sharpe_ok and ret_ok):
                    logger.warning("hpo_apply_blocked_by_non_regression", job_id=job_id)
                    return
        except Exception:
            pass
        # Proceed with HPO apply
        try:
            _apply_hpo_params_from_artifacts(evt)
        except Exception as e:
            logger.warning("hpo_apply_from_artifacts_failed", job_id=job_id, error=str(e))
    else:
        logger.info("success_noop_or_unhandled_type", job_id=job_id, job_type=job_type)

    # Run ML diagnostics for OOF/hpo_cli jobs (P2.3 integration)
    await _run_ml_diagnostics_and_execute(evt)

    # Set success cooldown locks to throttle further optimizations
    try:
        _set_success_cooldown(job_type, ttl_sec=6*3600)
        _set_success_cooldown("any", ttl_sec=6*3600)  # generic hint for orchestrator quiet sessions
    except Exception:
        pass

    # Write a lightweight hint for plans (optional, best-effort)
    try:
        d = _runs_dir() / date.today().isoformat() / "cache"
        d.mkdir(parents=True, exist_ok=True)
        (d / "plan_success_hint.json").write_text(json.dumps({
            "ts": datetime.now().isoformat(),
            "job_id": job_id,
            "job_type": job_type,
            "cooldown_sec": 6*3600,
            "metrics": comp_metrics,
            "gates": gates
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _success_cooldown_lock_path(job_type: str) -> Path:
    d = _runs_dir() / date.today().isoformat() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    safe = job_type.lower().replace("/", "_")
    return d / f"success_cooldown_{safe}.lock"


def _set_success_cooldown(job_type: str, ttl_sec: int = 21600) -> None:
    p = _success_cooldown_lock_path(job_type)
    # store payload for audit (mtime used for TTL)
    payload = {"ts": datetime.now().isoformat(), "ttl_sec": ttl_sec, "job_type": job_type}
    p.write_text(json.dumps(payload), encoding="utf-8")


def _is_success_cooldown_active(job_type: str) -> bool:
    import time as _t
    p = _success_cooldown_lock_path(job_type)
    if not p.exists():
        return False
    try:
        age = _t.time() - p.stat().st_mtime
        # read ttl if present
        ttl = 6*3600
        try:
            data = json.loads(p.read_text("utf-8"))
            ttl = int(data.get("ttl_sec", ttl))
        except Exception:
            pass
        return age < ttl
    except Exception:
        return False


async def _handle_ml_completed(evt: Dict[str, Any]) -> None:
    """Handle completed ML job event - trigger Brain review and save result.

    For POC: Only handle OOF completed events.
    Future: Handle HPO/RL completed and trigger plan refresh.
    """
    dstr = date.today().isoformat()
    job_id = evt.get("job_id", "unknown")
    job_type = evt.get("job_type", "unknown")
    metrics = evt.get("metrics", {})
    job_params = evt.get("params", {}) or {}

    logger.info("ml_job_completed_received", job_id=job_id, job_type=job_type, metrics=metrics)

    # For simple reward/credit assignment we keep a snapshot of previous and
    # current OOF metrics (per-symbol) so actions can see before/after state.
    prev_oof_metrics: Dict[str, Dict[str, float]] = {}
    cur_oof_metrics: Dict[str, Dict[str, float]] = {}

    # Build ENHANCED ML context for Brain with backtest results and config
    ml_ctx = {
        "date": dstr,
        "job_id": job_id,
        "job_type": job_type,
        "metrics": metrics,
        "job_params": job_params,
        "artifacts": evt.get("artifacts", []),
        "duration_sec": evt.get("duration_sec", 0),
        "ok": evt.get("ok", False)
    }

    # Attach static experiment policy (label/horizon grid) so Brain can use
    # a safe, operator-approved search space when designing OOF experiments
    # under long-run no-edge conditions.
    try:
        exp_cfg: Dict[str, Any] = {}
        mlp = Path("ml_policy.json")
        if mlp.exists():
            try:
                exp_all = json.loads(mlp.read_text("utf-8"))
                if isinstance(exp_all, dict):
                    exp_cfg = exp_all.get("experiment") or {}
            except Exception:
                exp_cfg = {}
        if exp_cfg:
            ml_ctx["experiment_policy"] = exp_cfg
    except Exception:
        pass

    # Basic data stats summary (best-effort)
    try:
        data_stats: Dict[str, Any] = {}
        n_bars = metrics.get("n_bars")
        if isinstance(n_bars, (int, float)):
            data_stats["n_bars"] = int(n_bars)
        # window_days from job params if available
        window_days = job_params.get("window_days") or job_params.get("days")
        if isinstance(window_days, (int, float)):
            data_stats["window_days"] = int(window_days)
        if data_stats:
            ml_ctx["data_stats"] = data_stats
    except Exception:
        pass
    # Attach last accepted HPO best_params snapshot (model hyperparameters) so
    # the Brain can reason about current XGBoost/ensemble settings even when
    # reviewing non-HPO jobs (e.g., OOF or alpha_loop).
    try:
        cache_dir = Path(_runs_dir()) / dstr / "cache"
        bp_fp = cache_dir / "hpo_last_best_params.json"
        if bp_fp.exists():
            try:
                bp = json.loads(bp_fp.read_text("utf-8"))
            except Exception:
                bp = None
            if isinstance(bp, dict) and bp:
                ml_ctx["hpo_last_best_params"] = bp
    except Exception:
        pass
    # Lightweight data quality summary for ML review (best-effort; does not replace full validators)
    try:
        dq: Dict[str, Any] = {}
        cov = metrics.get("coverage")
        ic = metrics.get("fusion_ic", metrics.get("ic"))
        try:
            cov_f = float(cov) if cov is not None else 0.0
        except Exception:
            cov_f = 0.0
        dq["coverage"] = cov_f
        if isinstance(ic, (int, float)):
            dq["fusion_ic"] = float(ic)
        if "n_bars" in (ml_ctx.get("data_stats") or {}):
            dq["n_bars"] = ml_ctx["data_stats"]["n_bars"]
        status = "ok"
        issues: List[str] = []
        if cov_f < 0.6:
            status = "bad"
            issues.append("low_coverage")
        elif cov_f < 0.9:
            status = "weak"
            issues.append("weak_coverage")
        if isinstance(ic, (int, float)):
            ic_f = float(ic)
            if abs(ic_f) < 0.001:
                issues.append("no_signal")
            elif 0 < ic_f <= 0.02:
                issues.append("weak_signal")
        dq["status"] = status
        if issues:
            dq["issues"] = issues
        # Optional: label-level health derived from label_profiles.yml and current profile
        try:
            label_health: Dict[str, Any] = {}
            lp_name = None
            try:
                lp_name = (ml_ctx.get("label_profile") or {}).get("name")
            except Exception:
                lp_name = None
            if lp_name:
                from pathlib import Path as _P
                import yaml as _yaml  # type: ignore
                lp_path = _P("label_profiles.yml")
                if lp_path.exists():
                    data = _yaml.safe_load(lp_path.read_text("utf-8")) or {}
                    profiles = (data.get("profiles") or {}) if isinstance(data, dict) else {}
                    prof = profiles.get(lp_name)
                    status_l = "ok"
                    l_issues: List[str] = []
                    # Flag overnight profiles explicitly
                    if str(lp_name).startswith("overnight_"):
                        status_l = "bad"
                        l_issues.append("overnight_profile_not_allowed")
                    # Horizon sanity for intraday objectives (treat > 60 bars as long)
                    try:
                        horizon = None
                        if isinstance(prof, dict):
                            horizon = prof.get("horizon_bars")
                        if isinstance(horizon, (int, float)) and horizon > 60:
                            if status_l != "bad":
                                status_l = "weak"
                            l_issues.append(f"horizon_long_for_intraday:{int(horizon)}")
                    except Exception:
                        pass
                    label_health["profile"] = lp_name
                    label_health["status"] = status_l
                    if l_issues:
                        label_health["issues"] = l_issues
                    dq["label_health"] = label_health
        except Exception:
            # Label health is best-effort; ignore failures
            pass
        ml_ctx["data_quality"] = dq
    except Exception:
        pass

    # Aggregate recent OOF metrics across symbols to give Brain a simple
    # view of current predictive power (AUC/IC/coverage) and a coarse
    # "no_edge" flag when long-run performance stays near random.
    try:
        # Load latest per-symbol OOF evaluations if available
        oof_eval_dir = Path(_runs_dir()) / dstr / "oof_eval"
        oof_evals: Dict[str, Any] = {}
        if oof_eval_dir.exists():
            for eval_file in oof_eval_dir.glob("*.json"):
                try:
                    symbol = eval_file.stem
                    with eval_file.open("r", encoding="utf-8") as f:
                        oof_evals[symbol] = json.load(f)
                except Exception:
                    continue

        # Summarize per-symbol metrics into a recent health snapshot
        ic_vals: List[float] = []
        auc_vals: List[float] = []
        cov_vals: List[float] = []
        for s in oof_evals.values():
            try:
                fusion = s.get("fusion") or {}
                ic = fusion.get("ic")
                auc = fusion.get("auc")
                cov = s.get("coverage")
                if isinstance(ic, (int, float)) and not (abs(ic) != ic or ic != ic):
                    ic_vals.append(float(ic))
                if isinstance(auc, (int, float)):
                    auc_vals.append(float(auc))
                if isinstance(cov, (int, float)):
                    cov_vals.append(float(cov))
            except Exception:
                continue

        if ic_vals or auc_vals or cov_vals:
            ml_health_recent: Dict[str, Any] = {}
            if ic_vals:
                ml_health_recent["fusion_ic_mean"] = sum(ic_vals) / len(ic_vals)
            if auc_vals:
                ml_health_recent["fusion_auc_mean"] = sum(auc_vals) / len(auc_vals)
            if cov_vals:
                ml_health_recent["coverage_mean"] = sum(cov_vals) / len(cov_vals)

            # Simple no-edge heuristic: AUC≈0.5 and |IC|≈0 over recent window.
            # Thresholds are configurable via ml_policy.json.no_edge.*.
            try:
                auc_near = float(ml_health_recent.get("fusion_auc_mean", 0.5))
                ic_near = float(ml_health_recent.get("fusion_ic_mean", 0.0))
                thr_auc = 0.02
                thr_ic = 0.01
                try:
                    mlp = Path("ml_policy.json")
                    if mlp.exists():
                        pol = json.loads(mlp.read_text("utf-8"))
                        no_edge_cfg = pol.get("no_edge") or {}
                        if isinstance(no_edge_cfg, dict):
                            if no_edge_cfg.get("auc_near_random_thresh") is not None:
                                thr_auc = float(no_edge_cfg["auc_near_random_thresh"])
                            if no_edge_cfg.get("ic_near_zero_thresh") is not None:
                                thr_ic = float(no_edge_cfg["ic_near_zero_thresh"])
                except Exception:
                    pass
                no_edge = (abs(auc_near - 0.5) < thr_auc) and (abs(ic_near) < thr_ic)
            except Exception:
                no_edge = False
            ml_health_recent["no_edge_simple"] = bool(no_edge)
            ml_ctx["ml_health_recent"] = ml_health_recent
    except Exception:
        # Aggregation is best-effort; ignore failures
        pass

    # Attach ML Gateway decision stats (best-effort; summarizes how often
    # gateway blocked trades vs allowed them for the *recent* window).
    try:
        gw_stats: Dict[str, Any] = {}
        gw_file = _runs_dir() / dstr / "ml_gateway_decisions.jsonl"
        if gw_file.exists():
            total_by_sym: Dict[str, int] = {}
            blocked_by_sym: Dict[str, int] = {}
            codes_by_sym: Dict[str, Dict[str, int]] = {}
            # Only consider recent gateway decisions so that historical
            # blocks (e.g. from earlier debugging sessions) do not cause
            # the Brain to believe the system is still hard-blocked.
            # Default recency window: last 60 minutes.
            now_utc = datetime.now(timezone.utc)
            recent_cutoff = now_utc - timedelta(minutes=60)
            for line in gw_file.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                # Parse timestamp; skip records without a usable ts so that
                # legacy entries (ts=null) do not dominate stats forever.
                ts_raw = row.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_raw))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    # If timestamp cannot be parsed, treat it as non-recent
                    # and exclude it from gateway_stats.
                    continue
                if ts < recent_cutoff:
                    continue
                sym = str(row.get("symbol") or "").strip()
                if not sym:
                    continue
                total_by_sym[sym] = total_by_sym.get(sym, 0) + 1
                decision = str(row.get("decision") or "")
                if decision == "blocked":
                    blocked_by_sym[sym] = blocked_by_sym.get(sym, 0) + 1
                    code = str(row.get("code") or "unknown")
                    codes = codes_by_sym.setdefault(sym, {})
                    codes[code] = codes.get(code, 0) + 1
            for sym, tot in total_by_sym.items():
                blk = blocked_by_sym.get(sym, 0)
                codes = codes_by_sym.get(sym, {})
                gw_stats[sym] = {
                    "total": tot,
                    "blocked": blk,
                    "block_ratio": float(blk) / float(tot) if tot > 0 else 0.0,
                    "block_codes": codes,
                }
        if gw_stats:
            ml_ctx["gateway_stats"] = gw_stats
    except Exception:
        # Gateway stats are optional diagnostics; ignore failures.
        pass

    # Summarize recent ML experiments (OOF runs) so Brain can reason about
    # whether different label/horizon configurations have produced any edge.
    try:
        exp_file = _runs_dir() / dstr / "ml_experiments.jsonl"
        total_exp = 0
        positive_exp = 0
        auc_vals: List[float] = []
        ic_vals: List[float] = []
        # Optional grouping by (window_days, horizon_min, label_bps) so Brain
        # can see which combos work best.
        combos: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        if exp_file.exists():
            for line in exp_file.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if (row.get("job_type") or "").lower() != "oof":
                    continue
                metrics_row = row.get("metrics") or {}
                params_row = row.get("params") or {}
                # Prefer fusion_* keys from nightly_oof_flow
                auc = metrics_row.get("fusion_auc")
                ic = metrics_row.get("fusion_ic")
                # Fallbacks for more generic shapes
                if not isinstance(auc, (int, float)):
                    auc = metrics_row.get("auc")
                if not isinstance(ic, (int, float)):
                    ic = metrics_row.get("ic")
                if not isinstance(auc, (int, float)) or not isinstance(ic, (int, float)):
                    continue
                try:
                    auc_f = float(auc)
                    ic_f = float(ic)
                except Exception:
                    continue
                total_exp += 1
                auc_vals.append(auc_f)
                ic_vals.append(ic_f)
                if (auc_f >= 0.52) and (abs(ic_f) >= 0.02):
                    positive_exp += 1
                # Try to extract combo coordinates
                try:
                    w = params_row.get("window_days", metrics_row.get("window_days"))
                    h = params_row.get("horizon_min", metrics_row.get("horizon_min"))
                    b = params_row.get("label_bps", metrics_row.get("label_bps"))
                    if w is None or h is None or b is None:
                        continue
                    key = (int(w), int(h), int(b))
                except Exception:
                    continue
                g = combos.setdefault(
                    key,
                    {"count": 0, "sum_auc": 0.0, "sum_ic": 0.0, "positive": 0},
                )
                g["count"] += 1
                g["sum_auc"] += auc_f
                g["sum_ic"] += ic_f
                if (auc_f >= 0.52) and (abs(ic_f) >= 0.02):
                    g["positive"] += 1
        if total_exp > 0:
            exp_summary: Dict[str, Any] = {
                "total_experiments": total_exp,
                "positive_edge_experiments": positive_exp,
            }
            if auc_vals:
                exp_summary["mean_auc"] = sum(auc_vals) / len(auc_vals)
            if ic_vals:
                exp_summary["mean_ic"] = sum(ic_vals) / len(ic_vals)
            # If we have run enough OOF experiments today and none show
            # meaningful edge, mark this as a stronger "no edge confirmed".
            min_exp = 10
            try:
                mlp = Path("ml_policy.json")
                if mlp.exists():
                    pol = json.loads(mlp.read_text("utf-8"))
                    ne_cfg = (pol.get("no_edge") or {})
                    if isinstance(ne_cfg, dict) and ne_cfg.get("min_experiments_for_confirm") is not None:
                        min_exp = int(ne_cfg["min_experiments_for_confirm"])
            except Exception:
                pass
            exp_summary["no_edge_confirmed"] = bool(total_exp >= min_exp and positive_exp == 0)
            ml_ctx["experiments_summary"] = exp_summary

            # Attach per-combo summary and best_combo for Brain. best_combo is
            # chosen by highest mean_auc then mean_ic among combos with at
            # least one positive-edge experiment if any; otherwise by mean_auc.
            if combos:
                by_combo: List[Dict[str, Any]] = []
                for (w, h, b), g in combos.items():
                    cnt = g["count"]
                    if cnt <= 0:
                        continue
                    mean_auc = g["sum_auc"] / cnt
                    mean_ic = g["sum_ic"] / cnt
                    by_combo.append(
                        {
                            "window_days": w,
                            "horizon_min": h,
                            "label_bps": b,
                            "count": cnt,
                            "positive": g["positive"],
                            "mean_auc": mean_auc,
                            "mean_ic": mean_ic,
                        }
                    )
                best_combo: Dict[str, Any] = {}
                if by_combo:
                    # Prefer combos with at least one positive edge if present
                    candidates = [c for c in by_combo if c["positive"] > 0] or by_combo
                    candidates.sort(
                        key=lambda c: (float(c.get("mean_auc", 0.0)), float(c.get("mean_ic", 0.0))),
                        reverse=True,
                    )
                    best_combo = candidates[0]
                ml_ctx["experiments_combos"] = {
                    "by_combo": by_combo,
                    "best_combo": best_combo,
                }

            # Attach vector-based recall of similar ML episodes for ML Brain.
            # This is best-effort: if embeddings/ClickHouse are unavailable,
            # similar_episodes simply remains empty and Brain falls back to
            # scalar diagnostics.
            try:
                from scripts.ai_trader.memory import vector_store as _vs  # type: ignore
                emb_meta = _vs.record_ml_episode_embedding(evt)
                emb_vec = emb_meta.get("embedding") if emb_meta else None
                similar = _vs.query_similar_episodes(evt, current_embedding=emb_vec, max_days=30, top_k=5)
                if similar:
                    ml_ctx["similar_episodes"] = similar
            except Exception as exc:
                logger.debug("ml_vector_memory_attach_failed", error=str(exc))
    except Exception:
        # Experiment summary is best-effort; ignore failures
        pass

    # Attach lightweight schema snapshots so Brain can see valid columns
    # for key feature tables (best-effort; failures are non-fatal).
    try:
        from libs.db.ch_pool import get_client as _ch_get_client

        cli = _ch_get_client()
        schemas: Dict[str, Any] = {}
        for tbl in ("trading.features_1m", "trading.features_5m"):
            try:
                # Robust execution: handle both .query() and .execute() if needed
                res = cli.query(f"DESCRIBE TABLE {tbl}")
                
                # Robust result extraction: handle QueryResult object vs List
                rows = []
                if hasattr(res, 'result_rows'):
                    rows = res.result_rows
                elif isinstance(res, list):
                    rows = res
                
                # Robust row parsing: handle Dict vs Tuple
                cols = []
                for r in rows:
                    name, typ = None, None
                    if isinstance(r, dict):
                        name = r.get("name") or r.get("column")
                        typ = r.get("type")
                    elif isinstance(r, (list, tuple)) and len(r) >= 2:
                        name = r[0]
                        typ = r[1]
                    
                    if name and typ:
                        cols.append({"name": str(name), "type": str(typ)})
                
                schemas[tbl] = cols
            except Exception as exc:
                logger.warning("ml_schema_inspect_failed", table=tbl, error=str(exc))
        if schemas:
            ml_ctx["schemas"] = schemas
    except Exception:
        # Schema context is optional
        pass

    # Add data availability information for Brain to know historical data range
    try:
        from libs.config import config as _cfg
        symbols = _cfg.universe or ["TSLA", "AAPL", "NVDA", "SPY"]

        # Query ClickHouse for data availability
        import subprocess
        ch_query = f"""
            SELECT
                symbol,
                dateDiff('day', MIN(ts), MAX(ts)) as days_available
            FROM trading.features_1m_with_labels
            WHERE symbol IN ({",".join(["'" + s + "'" for s in symbols[:10]])})
            GROUP BY symbol
            FORMAT JSONCompact
        """
        result = subprocess.run(
            ["docker", "exec", "-i", "trading_clickhouse", "clickhouse-client", "--query", ch_query],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            data_availability = {row[0]: row[1] for row in data.get("data", [])}
            ml_ctx["data_availability"] = {
                "symbols": data_availability,
                "max_days_available": max(data_availability.values()) if data_availability else 200,
                "note": "Historical data available in features_1m_with_labels table"
            }
    except Exception:
        # Fallback if query fails
        ml_ctx["data_availability"] = {
            "max_days_available": 200,
            "note": "Estimated based on TSLA data range (April-November 2025)"
        }

    # Add gap summary so Brain can see which days still lack bars and avoid blind backfill loops
    try:
        from tool.load_hist_multi import (  # type: ignore[import]
            HistoricalDataLoader,
            normalize_bar_size,
            table_name_for_bar,
        )

        # Use a conservative window; can be tuned via policies.ml.backfill_window_days
        try:
            pol_ml = ((_cfg.get("policies") or {}).get("ml") or {})
            window_days_cfg = int(pol_ml.get("backfill_window_days", 60) or 60)
        except Exception:
            window_days_cfg = 60

        bar_size = "1m"
        bar_size_norm = normalize_bar_size(bar_size)
        table = table_name_for_bar(bar_size_norm)

        loader = HistoricalDataLoader(
            ib_host="127.0.0.1",
            ib_port=4002,
            ib_client_id=99997,
        )

        today_local = datetime.now(loader.ib_timezone).date()
        window_days = max(1, window_days_cfg)
        start_day = today_local - timedelta(days=window_days - 1)
        end_day = today_local

        # Limit to a small subset of universe for gap summary (first 10)
        gap_syms = [str(s).upper() for s in (symbols or [])][:10]
        gap_entries = []
        for sym in gap_syms:
            try:
                incomplete = loader._get_incomplete_dates(  # type: ignore[attr-defined]
                    symbol=sym,
                    table=table,
                    bar_size=bar_size_norm,
                    start_day=start_day,
                    end_day=end_day,
                    min_coverage=0.98,
                )
            except Exception:
                continue
            if not incomplete:
                continue
            earliest = min(incomplete)
            latest = max(incomplete)
            gap_entries.append(
                {
                    "symbol": sym,
                    "gap_days": len(incomplete),
                    "earliest_gap": earliest.isoformat(),
                    "latest_gap": latest.isoformat(),
                }
            )

        if gap_entries:
            ml_ctx["data_gaps"] = {
                "window_days": window_days,
                "bar_size": bar_size_norm,
                "symbols": gap_entries,
            }
        else:
            # If no gaps found, still provide a small hint
            ml_ctx["data_gaps"] = {
                "window_days": window_days,
                "bar_size": bar_size_norm,
                "symbols": [],
                "note": "No missing days detected in recent window; backfill is likely up to date.",
            }
    except Exception:
        # Gap summary is best-effort; ignore failures
        pass

    # Call Brain.review_ml_async() to evaluate results
    try:
        global _orchestrator_instance
        if _orchestrator_instance is None:
            from scripts.ai_trader.orchestrator import Orchestrator
            _orchestrator_instance = Orchestrator()
        o = _orchestrator_instance
        if hasattr(o, 'brain') and hasattr(o.brain, 'review_ml_async'):
            # Load and attach optimization state to context so Brain can consider it
            try:
                ml_ctx["opt_state"] = _load_opt_state()
            except Exception:
                ml_ctx["opt_state"] = {"fail_streak": 0, "attempts": {"data": 0, "params": 0, "hpo": 0}}

            # Add backtest results and config for better diagnosis
            try:
                runs_dir = Path(o.runs_dir) / dstr

                # 1. Load backtest compare.json (trades, sharpe, etc)
                compare_file = runs_dir / "ensemble_compare" / "compare.json"
                if compare_file.exists():
                    with open(compare_file) as f:
                        ml_ctx["backtest_results"] = json.load(f)

                # 2. Load OOF eval results (mean_conf, std, etc)
                oof_eval_dir = runs_dir / "oof_eval"
                if oof_eval_dir.exists():
                    oof_evals = {}
                    for eval_file in oof_eval_dir.glob("*.json"):
                        symbol = eval_file.stem
                        with open(eval_file) as f:
                            oof_evals[symbol] = json.load(f)
                    ml_ctx["oof_evaluations"] = oof_evals

                    # 2b. Lightweight microstructure health snapshot from trading.features_5m
                    # This helps the Brain avoid over-investing in microstructure features
                    # when tick_imbalance is effectively constant/null for the symbols of
                    # interest (a common case when tick data is not available).
                    try:
                        if oof_evals:
                            try:
                                from libs.db.clickhouse import ClickHouseDB  # type: ignore
                            except Exception:
                                ClickHouseDB = None  # type: ignore[assignment]
                            if ClickHouseDB is not None:
                                try:
                                    ch = ClickHouseDB()
                                    syms = sorted({str(s) for s in oof_evals.keys() if s})
                                    if syms:
                                        # Build a safe IN (...) list
                                        in_list_parts = []
                                        for sym in syms:
                                            # Strip single quotes to avoid injection, then wrap again
                                            cleaned = str(sym).replace("'", "")
                                            in_list_parts.append("'" + cleaned + "'")
                                        in_list = ", ".join(in_list_parts)
                                        sql = f"""
                                        SELECT
                                            symbol,
                                            count() AS n,
                                            countIf(tick_imbalance != 0) AS nz,
                                            avg(tick_imbalance) AS avg_imb
                                        FROM trading.features_5m
                                        WHERE symbol IN ({in_list})
                                          AND ts >= now() - INTERVAL 60 DAY
                                        GROUP BY symbol
                                        """
                                        rows = ch.query(sql)
                                        micro: Dict[str, Any] = {
                                            "lookback_days": 60,
                                            "symbols": {},
                                        }
                                        max_frac = 0.0
                                        for r in rows:
                                            sym = str(r.get("symbol"))
                                            n = float(r.get("n") or 0.0)
                                            nz = float(r.get("nz") or 0.0)
                                            frac = (nz / n) if n > 0 else 0.0
                                            max_frac = max(max_frac, frac)
                                            micro["symbols"][sym] = {
                                                "nonzero_frac": frac,
                                                "avg_tick_imbalance": r.get("avg_imb"),
                                            }
                                        micro["tick_imbalance_has_signal"] = bool(
                                            max_frac > 0.01
                                        )
                                        ml_ctx["microstructure_health"] = micro
                                except Exception as exc:
                                    logger.debug(
                                        "microstructure_health_compute_failed",
                                        error=str(exc),
                                    )
                    except Exception:
                        # Microstructure health is best-effort; ignore failures
                        pass

                # 3. Load gating_policies.json (conf_floor thresholds)
                gating_file = Path("config/gating_policies.json")
                if gating_file.exists():
                    with open(gating_file) as f:
                        ml_ctx["gating_policies"] = json.load(f)

                # 4. Load per-symbol strategy configs (may override gating policies)
                strategy_configs = {}
                config_dir = Path("strategies/configs")
                if config_dir.exists():
                    for cfg_file in config_dir.glob("enhanced_ml_*.json"):
                        symbol = cfg_file.stem.replace("enhanced_ml_", "")
                        try:
                            with open(cfg_file) as f:
                                cfg = json.load(f)
                                # Extract only relevant parts to avoid bloat
                                strategy_configs[symbol] = {
                                    "conf_floor": cfg.get("market_filter", {}).get("conf_floor"),
                                    "adaptive_rules": cfg.get("adaptive", {}).get("rules", [])
                                }
                        except Exception:
                            pass
                    if strategy_configs:
                        ml_ctx["strategy_configs"] = strategy_configs

                # 5. Surface feature_build status and data_quality recency for Brain
                try:
                    # feature_build status.json
                    fb_status_fp = runs_dir / "features_build" / "status.json"
                    if fb_status_fp.exists():
                        fb_payload = json.loads(fb_status_fp.read_text("utf-8"))
                        fb_payload["path"] = str(fb_status_fp)
                        try:
                            mtime = fb_status_fp.stat().st_mtime
                            fb_payload["age_min"] = (datetime.now(timezone.utc).timestamp() - mtime) / 60.0
                        except Exception:
                            pass
                        ml_ctx["feature_build_status"] = fb_payload

                    # data_quality.json age + coverage snapshot
                    dq_fp = runs_dir / "data_quality.json"
                    if dq_fp.exists():
                        try:
                            dq_data = json.loads(dq_fp.read_text("utf-8"))
                        except Exception:
                            dq_data = {}
                        ml_ctx.setdefault("data_quality_snapshot", dq_data)
                        try:
                            mtime = dq_fp.stat().st_mtime
                            ml_ctx["data_quality_age_min"] = (datetime.now(timezone.utc).timestamp() - mtime) / 60.0
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug("feature_build_status_load_failed", error=str(e))

                # 5. Attach a lightweight snapshot of HPO/HPO-CLI config when available
                try:
                    # For hpo_cli jobs, we can inspect the config file used to run wf_hpo
                    if job_type == "hpo_cli":
                        cfg_snapshot: Dict[str, Any] = {}
                        # job_params may be nested; align with _execute_hpo_cli usage
                        raw_params = job_params if isinstance(job_params, dict) else {}
                        cfg_path = raw_params.get("config")
                        if cfg_path:
                            from pathlib import Path as _P
                            import json as _J
                            p = _P(cfg_path)
                            if not p.is_absolute():
                                # Treat as project-root relative
                                project_root = _P(__file__).resolve().parents[2]
                                p = project_root / p
                            if p.exists():
                                try:
                                    cfg = _J.loads(p.read_text("utf-8"))
                                except Exception:
                                    cfg = {}
                                if isinstance(cfg, dict):
                                    cfg_snapshot["label_profile"] = cfg.get("label_profile")
                                    cfg_snapshot["features_table"] = cfg.get("features_table")
                                    if "model_pool" in cfg:
                                        cfg_snapshot["model_pool"] = cfg.get("model_pool")
                                    hpo_cfg = cfg.get("hpo") or {}
                                    if isinstance(hpo_cfg, dict):
                                        grid = hpo_cfg.get("grid")
                                        optuna_space = hpo_cfg.get("optuna_space")
                                        snap_hpo: Dict[str, Any] = {}
                                        if isinstance(grid, dict):
                                            snap_hpo["grid_keys"] = sorted(list(grid.keys()))
                                        if optuna_space is not None:
                                            snap_hpo["has_optuna_space"] = True
                                        if snap_hpo:
                                            cfg_snapshot["hpo"] = snap_hpo
                                if cfg_snapshot:
                                    ml_ctx["hpo_config"] = cfg_snapshot
                                    # Promote commonly used fields for easier ML review
                                    label_profile = cfg_snapshot.get("label_profile")
                                    if label_profile:
                                        ml_ctx["label_profile"] = {"name": label_profile}
                                    model_space_summary: Dict[str, Any] = {}
                                    if isinstance(hpo_cfg, dict):
                                        # model_type choices if present in grid/optuna_space
                                        try:
                                            opt_space = hpo_cfg.get("optuna_space") or {}
                                            mt = opt_space.get("model_type") or {}
                                            if isinstance(mt, dict) and isinstance(mt.get("choices"), list):
                                                model_space_summary["model_type_choices"] = list(mt["choices"])
                                        except Exception:
                                            pass
                                    if "model_pool" in cfg_snapshot:
                                        model_space_summary["has_model_pool"] = True
                                    if model_space_summary:
                                        ml_ctx["model_space_summary"] = model_space_summary
                except Exception as e:
                    logger.warning("failed_to_enrich_ml_ctx_hpo_config", error=str(e))

            except Exception as e:
                logger.warning("failed_to_load_additional_context", error=str(e))

            # Stage 0: deterministic pre-check & allowlists
            try:
                from scripts.ai_trader.brain.policy.validators import build_ml_stage0, validate_llm_review_output
                # Attach runs_dir for validator recent_actions lookup
                ml_ctx["runs_dir"] = str(o.runs_dir)
                stage0 = build_ml_stage0(ml_ctx)
                ml_ctx["stage0"] = stage0
                # Optional feature summary for Alpha/Model experts (best-effort)
                try:
                    feat_summary: Dict[str, Any] = {}
                    # 1) Feature_catalog-based tags: count candidate/active features
                    try:
                        fc_path = Path("config/features_catalog.json")
                        if fc_path.exists():
                            fc = json.loads(fc_path.read_text("utf-8"))
                            active = fc.get("active") or []
                            cand = fc.get("candidate") or []
                            feat_summary["count_active"] = len(active) if isinstance(active, list) else 0
                            feat_summary["count_candidate"] = len(cand) if isinstance(cand, list) else 0
                    except Exception:
                        pass
                    # 2) Lightweight tag summary derived from feature names (if oof_evaluations present)
                    try:
                        tags: Dict[str, int] = {}
                        oof_eval = ml_ctx.get("oof_evaluations") or {}
                        for sym_vals in (oof_eval.values() if isinstance(oof_eval, dict) else []):
                            try:
                                cols = sym_vals.get("feature_cols") or []
                                for c in cols:
                                    cs = str(c)
                                    if "vwap" in cs:
                                        tags["vwap"] = tags.get("vwap", 0) + 1
                                    if "vol" in cs or "volume" in cs:
                                        tags["vol"] = tags.get("vol", 0) + 1
                                    if "open" in cs or "opening" in cs:
                                        tags["opening"] = tags.get("opening", 0) + 1
                                    if "overnight" in cs:
                                        tags["overnight"] = tags.get("overnight", 0) + 1
                            except Exception:
                                continue
                        if tags:
                            feat_summary["tags"] = tags
                    except Exception:
                        pass
                    if feat_summary:
                        ml_ctx["feature_summary"] = feat_summary
                except Exception:
                    pass
            except Exception as e:
                logger.warning("ml_stage0_failed", error=str(e))
                stage0 = {
                    "derived_flags": {},
                    "allowed_actions": [
                        "backfill",
                        "propose_features",
                        "extend_window",
                        "queue_job",
                        "fix_config",
                        "pause_ml",
                        "resume_ml",
                        "reduce_window",
                        "data_quality_check",
                        "feature_recompute",
                        "wait",
                        "do_nothing",
                        "escalate",
                    ],
                    "allowed_numbers": [],
                    "recent_actions": [],
                    "missing_fields": [],
                }

            # If critical fields missing: skip LLM and request context/wait
            if stage0.get("missing_fields"):
                review_result = {
                    "diagnosis": [f"insufficient_context: missing {','.join(stage0['missing_fields'])}"],
                    "next_actions": [{"action": "wait", "confidence": 0.9, "params": {"cycles": 1}}],
                    "status": "insufficient_context",
                    "prev_attempts_summary": ", ".join([str(a.get("action")) for a in stage0.get("recent_actions", [])])
                }
            else:
                # Multi-turn ML review with optional tool calls. In each turn:
                #   1) Call Brain.review_ml_async(ml_ctx_cur)
                #   2) If output.tool_calls present, execute supported tools
                #      (inspect_table_schema, dry_run_feature_expr), attach
                #      results as INPUT.tool_results, and call Brain again.
                #   3) Stop when no tool_calls or max_turns reached.
                ml_ctx_cur = dict(ml_ctx)

                # 🆕 P0 UPGRADE: Deep OOF Failure Diagnosis
                # When OOF performs poorly (AUC < 0.55 or IC < 0.1), run automated
                # diagnosis to identify root cause (config mismatch, low data utilization,
                # label quality issues) and attach to ml_ctx for Brain review.
                if job_type == "oof":
                    auc = metrics.get("xgb_auc", metrics.get("fusion_auc", 0.0))
                    ic = metrics.get("xgb_ic", metrics.get("fusion_ic", 0.0))
                    n_train = metrics.get("xgb_n", metrics.get("n_bars", 0))

                    # Trigger diagnosis for poor performance
                    if auc < 0.55 or (ic is not None and abs(ic) < 0.1):
                        logger.info("oof_poor_performance_diagnosis_triggered",
                                    job_id=job_id, auc=auc, ic=ic, n_train=n_train)

                        try:
                            from tools.diagnose_oof_failure import diagnose_oof_failure

                            # Get required params
                            horizon_min = metrics.get("horizon_min") or job_params.get("horizon_min", 30)
                            symbols = job_params.get("symbols", ["TSLA"])
                            symbol = symbols[0] if symbols else "TSLA"

                            # Get time range (default to last 60 days), but clamp to last data day to avoid empty holiday tails.
                            def _last_data_day():
                                try:
                                    from libs.utils.market_calendar import last_data_day
                                    return last_data_day(symbols)
                                except Exception:
                                    return None

                            # Resolve end/start with clamp to last data day even if caller provided start/end
                            window_days_default = int(job_params.get("window_days", 60) or 60)
                            end_param = job_params.get("end_override") or job_params.get("end")
                            try:
                                end_dt = datetime.fromisoformat(str(end_param)) if end_param else datetime.now()
                            except Exception:
                                end_dt = datetime.now()

                            data_day = _last_data_day()
                            if not data_day:
                                logger.warning("diagnosis_aborted_no_bars", job_id=job_id)
                                return

                            anchor_day = data_day
                            end_dt = datetime.combine(anchor_day, datetime.strptime("16:00:00", "%H:%M:%S").time())
                            logger.info(
                                "diagnosis_end_clamped",
                                job_id=job_id,
                                anchor_day=anchor_day.isoformat(),
                                source="data_day",
                                original_end=str(end_param) if end_param else "now",
                            )

                            # Determine start (respect overrides)
                            start_param = job_params.get("start_override") or job_params.get("start")
                            try:
                                start_dt = datetime.fromisoformat(str(start_param)) if start_param else None
                            except Exception:
                                start_dt = None
                            if not start_dt:
                                start_dt = end_dt - timedelta(days=window_days_default)
                            elif start_dt > end_dt:
                                start_dt = end_dt - timedelta(days=window_days_default)
                                logger.info(
                                    "diagnosis_start_shifted_due_to_end_clamp",
                                    job_id=job_id,
                                    start=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    end=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    window_days=window_days_default,
                                )

                            start = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                            end = end_dt.strftime("%Y-%m-%d %H:%M:%S")

                            # If anchor day is stale, short-circuit to avoid infinite empty loops
                            try:
                                last_day_for_guard = anchor_day
                                if last_day_for_guard and (datetime.now().date() - last_day_for_guard).days > 1:
                                    logger.warning(
                                        "oof_diagnosis_aborted_missing_bars",
                                        job_id=job_id,
                                        last_data_day=last_day_for_guard.isoformat(),
                                    )
                                    return
                            except Exception:
                                pass

                            # Data quality gate: quick RTH/coverage check before diagnosis to avoid empty holidays
                            try:
                                import subprocess
                                dq_cmd = [
                                    sys.executable,
                                    "tools/data_quality_check.py",
                                    "--symbols",
                                    ",".join(symbols),
                                    "--days",
                                    str(window_days_default),
                                    "--json",
                                ]
                                dq_out = subprocess.run(dq_cmd, capture_output=True, text=True)
                                dq_json = None
                                if dq_out.stdout:
                                    import json as _json
                                    try:
                                        dq_json = _json.loads(dq_out.stdout)
                                    except Exception:
                                        dq_json = None
                                unhealthy = False
                                if dq_json and isinstance(dq_json, dict):
                                    for item in dq_json.get("symbols", []):
                                        if item.get("status") in ("low_coverage", "off_rth_excess", "no_data"):
                                            unhealthy = True
                                if unhealthy:
                                    logger.warning("oof_diagnosis_aborted_data_quality", job_id=job_id, dq=dq_json)
                                    return
                            except Exception:
                                pass

                            # Run diagnosis
                            ev_est = metrics.get("ev") or metrics.get("expected_value")
                            beta_corr = metrics.get("beta_corr") or metrics.get("corr_to_spy") or metrics.get("beta")
                            oof_result = {
                                "xgb_auc": auc,
                                "xgb_ic": ic,
                                "xgb_n": n_train,
                                "horizon_min": horizon_min,
                                "start_override": job_params.get("start_override"),
                                "end_override": job_params.get("end_override"),
                                "ev": ev_est,
                                "beta_corr": beta_corr,
                            }

                            ft_for_diag = job_params.get("features_table_override") or job_params.get("features_table")
                            if not ft_for_diag:
                                ft_for_diag = "trading.features_1m_with_labels"

                            diagnosis = diagnose_oof_failure(
                                job_id=job_id,
                                oof_result=oof_result,
                                symbol=symbol,
                                start=start,
                                end=end,
                                features_table=ft_for_diag,
                                output_file=None  # Don't write to file, just return
                            )

                            # Phase 4: diagnosis is now a Diagnosis object (not dict)
                            # Attach diagnosis to ml_ctx for Brain
                            ml_ctx_cur["oof_diagnosis"] = diagnosis.to_dict()

                            # Phase 4: Enhanced logging with standard protocol
                            logger.info("oof_diagnosis_completed",
                                        job_id=job_id,
                                        diagnosis_id=diagnosis.diagnosis_id,
                                        primary_root_cause=diagnosis.primary_root_cause.type.value if diagnosis.primary_root_cause else "unknown",
                                        num_issues=len(diagnosis.issues),
                                        num_root_causes=len(diagnosis.root_causes),
                                        num_actions=len(diagnosis.recommended_actions),
                                        confidence=diagnosis.confidence)

                            # Phase 4: Auto-healing logic (if enabled)
                            policies_cfg = (getattr(o, "cfg", {}) or {}).get("policies", {}) if hasattr(o, "cfg") else {}
                            auto_heal_enabled = bool(policies_cfg.get("auto_heal_enabled", False))
                            if auto_heal_enabled and diagnosis.recommended_actions:
                                try:
                                    from libs.healer import DataHealerV2

                                    # Get auto-executable actions
                                    auto_actions = diagnosis.get_auto_executable_actions()

                                    if auto_actions:
                                        logger.info("auto_heal_triggered",
                                                    job_id=job_id,
                                                    num_actions=len(auto_actions),
                                                    primary_root_cause=diagnosis.primary_root_cause.type.value if diagnosis.primary_root_cause else "unknown")

                                        # Execute auto-healing
                                        healer = DataHealerV2(runs_dir=str(o.runs_dir), dry_run=False)
                                        heal_results = healer.heal(diagnosis)

                                        # Log results
                                        success_count = sum(1 for r in heal_results if r.success)
                                        logger.info("auto_heal_completed",
                                                    job_id=job_id,
                                                    total_actions=len(heal_results),
                                                    successful=success_count,
                                                    failed=len(heal_results) - success_count)

                                        # Record to ml_events
                                        with open(f"{o.runs_dir}/ml_events.jsonl", "a") as f:
                                            f.write(json.dumps({
                                                "event": "auto_heal_completed",
                                                "timestamp": datetime.now().isoformat(),
                                                "job_id": job_id,
                                                "diagnosis_id": diagnosis.diagnosis_id,
                                                "primary_root_cause": diagnosis.primary_root_cause.type.value if diagnosis.primary_root_cause else "unknown",
                                                "actions": [r.to_dict() for r in heal_results],
                                                "success_count": success_count
                                            }) + "\n")

                                        # Attach heal results to ml_ctx for Brain
                                        ml_ctx_cur["heal_result"] = {
                                            "status": "fixed" if success_count == len(heal_results) else "partial",
                                            "actions": [r.to_dict() for r in heal_results],
                                            "success_count": success_count,
                                            "total_count": len(heal_results)
                                        }

                                    else:
                                        logger.info("auto_heal_skipped",
                                                    job_id=job_id,
                                                    reason="no_auto_executable_actions",
                                                    total_actions=len(diagnosis.recommended_actions))

                                except Exception as heal_exc:
                                    logger.error("auto_heal_failed",
                                                 job_id=job_id,
                                                 error=str(heal_exc),
                                                 exc_info=True)
                                    ml_ctx_cur["heal_result"] = {
                                        "status": "failed",
                                        "error": str(heal_exc)
                                    }
                            else:
                                if not auto_heal_enabled:
                                    logger.info("auto_heal_disabled", job_id=job_id)

                        except Exception as e:
                            logger.error("oof_diagnosis_failed",
                                         job_id=job_id, error=str(e), exc_info=True)

                review_result = {}
                max_turns = 3
                for turn in range(max_turns):
                    review_result = await o.brain.review_ml_async(ml_ctx_cur)
                    if not isinstance(review_result, dict):
                        break
                    tool_calls = review_result.get("tool_calls") or []
                    if not tool_calls:
                        break
                tool_results = []
                abort_tools = False
                for call in tool_calls:
                    tool_name = str(call.get("tool") or "")
                    args = call.get("args") or {}
                    required = bool(call.get("required"))
                    retryable = bool(call.get("retryable"))
                    on_error = (call.get("on_error") or "continue").lower()
                    res = {
                        "tool": tool_name,
                        "args": args,
                        "required": required,
                        "retryable": retryable,
                        "on_error": on_error,
                        "ok": False,
                    }
                    try:
                        if tool_name == "inspect_table_schema":
                            from scripts.ai_trader.brain.tools_sql import inspect_table_schema as _inspect
                            table = str(args.get("table") or "trading.features_1m")
                            out = _inspect(table, save=True)
                            res["ok"] = True
                            res["result"] = out
                        elif tool_name == "dry_run_feature_expr":
                            from scripts.ai_trader.brain.tools_sql import dry_run_feature_expr as _dry
                            expr = str(args.get("expr") or "")
                            sources = args.get("sources") or []
                            sample_symbol = str(args.get("symbol") or "TSLA")
                            try:
                                limit = int(args.get("limit", 10))
                            except Exception:
                                limit = 10
                            out = _dry(
                                expr,
                                sources=sources,
                                sample_symbol=sample_symbol,
                                limit=limit,
                                save=True,
                            )
                            res["ok"] = True
                            res["result"] = out
                        elif tool_name == "list_recent_jobs":
                            from scripts.ai_trader.brain import tools_ml as _tml
                            job_type = args.get("job_type")
                            try:
                                limit = int(args.get("limit", 20))
                            except Exception:
                                limit = 20
                            out = _tml.list_recent_jobs(job_type=job_type, limit=limit)
                            res["ok"] = True
                            res["result"] = out
                        elif tool_name == "get_job_details":
                            from scripts.ai_trader.brain import tools_ml as _tml
                            job_id = str(args.get("job_id") or "")
                            out = _tml.get_job_details(job_id)
                            res["ok"] = True
                            res["result"] = out
                        elif tool_name == "list_valid_experiment_ids":
                            from scripts.ai_trader.brain import tools_ml as _tml
                            try:
                                limit = int(args.get("limit", 50))
                            except Exception:
                                limit = 50
                            out = _tml.list_valid_experiment_ids(limit=limit)
                            res["ok"] = True
                            res["result"] = out
                        else:
                            res["error"] = "unknown_tool"
                    except Exception as exc:
                        res["error"] = str(exc)
                    tool_results.append(res)
                    if not res["ok"] and required and on_error == "abort":
                        abort_tools = True
                        break
                # Attach tool_results for next turn
                ml_ctx_cur = dict(ml_ctx_cur)
                if tool_results:
                    ml_ctx_cur["tool_results"] = tool_results
                if abort_tools:
                    # break # Removed because it was outside a loop, causing SyntaxError
                    pass # Placeholder to keep the if block valid

            # Save review result
            review_file = _runs_dir() / dstr / "ml_review.json"
            review_file.parent.mkdir(parents=True, exist_ok=True)
            with review_file.open("w") as f:
                json.dump({"job_id": job_id, "job_type": job_type, "review": review_result}, f, indent=2)
            logger.info("ml_review_saved", job_id=job_id, file=str(review_file))

            # Persist current OOF metrics for next cycle delta computation
            try:
                def _extract_oof_metrics(oof: Dict[str, Any] | None) -> Dict[str, Dict[str, float]]:
                    out: Dict[str, Dict[str, float]] = {}
                    if not isinstance(oof, dict):
                        return out
                    for sym, s in oof.items():
                        try:
                            if not isinstance(s, dict):
                                continue
                            # n_bars from params or top-level
                            n_bars = None
                            if isinstance(s.get("params"), dict) and isinstance(s["params"].get("n_bars"), (int, float)):
                                n_bars = float(s["params"]["n_bars"]) 
                            else:
                                for k in ("n_bars", "bars", "total_bars"):
                                    if isinstance(s.get(k), (int, float)):
                                        n_bars = float(s[k]); break
                            # ic/auc/coverage from fusion preferred
                            ic = None
                            auc = None
                            cov = None
                            if isinstance(s.get("fusion"), dict):
                                ic = s["fusion"].get("ic")
                                auc = s["fusion"].get("auc")
                                cov = s["fusion"].get("coverage", s["fusion"].get("cov"))
                            for m in ("xgb", "lstm"):
                                if ic is None and isinstance(s.get(m), dict):
                                    ic = s[m].get("ic")
                                if auc is None and isinstance(s.get(m), dict):
                                    auc = s[m].get("auc")
                                if cov is None and isinstance(s.get(m), dict):
                                    cov = s[m].get("coverage", s[m].get("cov"))
                            if n_bars is not None or ic is not None or auc is not None or cov is not None:
                                out[str(sym)] = {
                                    k: float(v) for k, v in {
                                        "n_bars": n_bars, "ic": ic, "auc": auc, "coverage": cov
                                    }.items() if v is not None
                                }
                        except Exception:
                            continue
                    return out

                prev_fp = _runs_dir() / dstr / "cache" / "ml_last_metrics.json"
                prev_fp.parent.mkdir(parents=True, exist_ok=True)
                # Load previous metrics snapshot if present
                try:
                    if prev_fp.exists():
                        prev_payload = json.loads(prev_fp.read_text("utf-8"))
                        prev_data = prev_payload.get("by_symbol") or {}
                        if isinstance(prev_data, dict):
                            prev_oof_metrics = {
                                str(sym): {
                                    k: float(v)
                                    for k, v in (vals or {}).items()
                                    if isinstance(v, (int, float))
                                }
                                for sym, vals in prev_data.items()
                            }
                except Exception:
                    prev_oof_metrics = {}

                cur_oof_metrics = _extract_oof_metrics(ml_ctx.get("oof_evaluations"))
                payload = {"ts": datetime.now().isoformat(), "by_symbol": cur_oof_metrics}
                prev_fp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                logger.info("ml_last_metrics_updated", file=str(prev_fp))
            except Exception as e:
                logger.warning("ml_last_metrics_update_failed", error=str(e))

            # Update optimization fail_streak based on completed result
            try:
                st = _load_opt_state()
                if bool(evt.get("ok", False)):
                    st["fail_streak"] = 0
                else:
                    st["fail_streak"] = int(st.get("fail_streak", 0)) + 1
                _save_opt_state(st)
            except Exception:
                pass

            # Tiered governance on failure: extend window / adjust gating
            try:
                st2 = _load_opt_state()
                fs = int(st2.get("fail_streak", 0))
                if not bool(evt.get("ok", False)):
                    # 1) On 2nd consecutive failure, suggest extend_window=45 via hint file
                    if fs == 2:
                        hints_file = _runs_dir() / dstr / "hpo_hints.json"
                        try:
                            hints = {}
                            if hints_file.exists():
                                hints = json.loads(hints_file.read_text("utf-8"))
                            hints["window_days"] = 45
                            hints["ts"] = datetime.now().isoformat()
                            hints["reason"] = "fail_streak_2"
                            hints_file.write_text(json.dumps(hints, indent=2), encoding="utf-8")
                            logger.info("fail_governance_extend_window_hint", window_days=45)
                        except Exception:
                            pass
                    # 2) On 3rd failure, gently lower gating conf_floor to widen exploration (min 0.45)
                    if fs == 3:
                        try:
                            from scripts.ai_trader.config import json_policy_patcher
                            cur = json_policy_patcher.get("config/gating_policies.json", "defaults.conf_floor")
                            if isinstance(cur, (int, float)):
                                new = max(0.45, float(cur) - 0.05)
                            else:
                                new = 0.55
                            upd = json_policy_patcher.update("config/gating_policies.json", "defaults.conf_floor", float(new))
                            if upd.get("ok"):
                                logger.info("fail_governance_gating_lowered", conf_floor=new)
                        except Exception:
                            pass
                    # 3) On 4th failure, trigger safe evolver sandbox for dummy skill (no production impact)
                    if fs == 4 or _should_trigger_evolver(st2):
                        try:
                            import yaml
                            import subprocess
                            evo_cfg = {}
                            policy_path = Path("config/evolution_policy.yaml")
                            if policy_path.exists():
                                evo_cfg = yaml.safe_load(policy_path.read_text()) or {}
                            targets = evo_cfg.get("target_skills") or ["dummy_math"]
                            skill = targets[0]
                            # Log the trigger to local evolution_experiments.jsonl
                            evolver_log = _runs_dir() / "evolution_experiments.jsonl"
                            evolver_log.parent.mkdir(parents=True, exist_ok=True)
                            exp_row = {
                                "ts": datetime.now().isoformat(),
                                "experiment_id": f"failstreak-{fs}",
                                "target_skill": skill,
                                "reason": "fail_streak",
                                "fail_streak": fs,
                            }
                            with evolver_log.open("a", encoding="utf-8") as f:
                                f.write(json.dumps(exp_row) + "\n")

                            env = os.environ.copy()
                            env["PYTHONPATH"] = str(_PROJECT_ROOT)
                            subprocess.run(
                                [
                                    sys.executable,
                                    "scripts/ai_trader/evolvers/code_evolver_git.py",
                                    "--skill",
                                    skill,
                                ],
                                cwd=str(_PROJECT_ROOT),
                                check=False,
                                env=env,
                            )
                            logger.info("fail_governance_evolver_triggered", skill=skill)
                        except Exception:
                            pass
            except Exception:
                pass

            # Success gates evaluation + auto-apply (best-effort)
            try:
                await _evaluate_and_apply_on_success(evt)
            except Exception as e:
                logger.error("success_gate_eval_failed", job_id=job_id, error=str(e), exc_info=True)

            # Lightweight experiment logging for OOF jobs so Brain can analyze
            # label/horizon/feature experiments over time without querying
            # ClickHouse directly. This is intentionally simple and append-only.
            try:
                if job_type == "oof":
                    exp_file = _runs_dir() / dstr / "ml_experiments.jsonl"
                    exp_file.parent.mkdir(parents=True, exist_ok=True)
                    exp_row: Dict[str, Any] = {
                        "ts": datetime.now().isoformat(),
                        "job_id": job_id,
                        "job_type": job_type,
                        "params": job_params,
                        "metrics": metrics,
                    }
                    # Attach a minimal view of recent health if available
                    if isinstance(ml_ctx.get("ml_health_recent"), dict):
                        exp_row["ml_health_recent"] = ml_ctx["ml_health_recent"]
                    # Attach current feature family state (if configured) so that
                    # later analysis can correlate label/horizon experiments with
                    # which feature families were enabled at the time.
                    try:
                        fam_path = Path("config/feature_families.json")
                        if fam_path.exists():
                            fam_data = json.loads(fam_path.read_text("utf-8"))
                            fams = fam_data.get("families") or {}
                            enabled: List[str] = []
                            disabled: List[str] = []
                            if isinstance(fams, dict):
                                for name, cfg in fams.items():
                                    try:
                                        if not isinstance(cfg, dict):
                                            continue
                                        if bool(cfg.get("enabled", True)):
                                            enabled.append(str(name))
                                        else:
                                            disabled.append(str(name))
                                    except Exception:
                                        continue
                            if enabled or disabled:
                                exp_row["feature_families"] = {
                                    "enabled": sorted(enabled),
                                    "disabled": sorted(disabled),
                                }
                    except Exception:
                        # Family state is best-effort; ignore failures
                        pass
                    # Best-effort write; failures are non-fatal
                    with exp_file.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(exp_row, ensure_ascii=False) + "\n")
                    logger.info("ml_experiment_logged", job_id=job_id, file=str(exp_file))
            except Exception as e:
                logger.warning("ml_experiment_log_failed", job_id=job_id, error=str(e))

            # Validate against stage0 allowlists, then translate and execute.
            # Before execution, we may enrich propose_features actions with
            # LLM-designed specs via a focused feature-catalog prompt.
            try:
                from scripts.ai_trader.brain.policy.validators import validate_llm_review_output
                allowed_actions = (stage0 or {}).get("allowed_actions") or []
                allowed_numbers = (stage0 or {}).get("allowed_numbers") or []
                ok, reason = validate_llm_review_output(review_result if isinstance(review_result, dict) else {}, allowed_actions, allowed_numbers)
                if not ok:
                    logger.warning("ml_review_validation_failed", job_id=job_id, reason=reason)
                    # Write audit and skip execution
                    try:
                        audit_file = _runs_dir() / dstr / "ml_actions.jsonl"
                        audit_file.parent.mkdir(parents=True, exist_ok=True)
                        if not audit_file.exists():
                            audit_file.touch()
                        with audit_file.open("a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "ts": datetime.now().isoformat(),
                                "job_id": job_id,
                                "action": "validation_failed",
                                "params": {"reason": reason},
                                "confidence": 0.0,
                                "status": "skipped"
                            }, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                    return
                translated = _translate_brain_actions(review_result.get("next_actions", []) if isinstance(review_result, dict) else [])

                # Optional second-stage LLM call: if Brain chose propose_features
                # without concrete specs, ask a focused feature-catalog agent to
                # design specs using only schemas/health/gaps context.
                async def _enrich_propose_features_with_specs(
                    orchestrator: Any,
                    ml_ctx_local: Dict[str, Any],
                    stage0_local: Dict[str, Any],
                    actions: list[Dict[str, Any]],
                ) -> list[Dict[str, Any]]:
                    brain = getattr(orchestrator, "brain", None)
                    if not brain or not hasattr(brain, "design_feature_specs_async"):
                        return actions
                    enriched: list[Dict[str, Any]] = []
                    for a in actions:
                        try:
                            if str(a.get("action") or "") != "propose_features":
                                enriched.append(a)
                                continue
                            params = a.get("params") or {}
                            specs = params.get("specs") or []
                            # Only trigger second-stage design when specs are missing;
                            # if Brain already provided specs we keep them.
                            if specs:
                                enriched.append(a)
                                continue
                            # Build a focused context for feature design
                            design_ctx: Dict[str, Any] = {
                                "schemas": ml_ctx_local.get("schemas"),
                                "ml_health_recent": ml_ctx_local.get("ml_health_recent"),
                                "data_gaps": ml_ctx_local.get("data_gaps"),
                                "data_quality_snapshot": ml_ctx_local.get("data_quality_snapshot"),
                                "feature_build_status": ml_ctx_local.get("feature_build_status"),
                                "stage0": {
                                    "recent_actions": stage0_local.get("recent_actions"),
                                    "derived_flags": stage0_local.get("derived_flags"),
                                },
                            }
                            design = await brain.design_feature_specs_async(design_ctx)
                            new_specs = design.get("specs") or []
                            if new_specs:
                                params["specs"] = new_specs
                                if design.get("version"):
                                    params.setdefault("version", design["version"])
                                a["params"] = params
                            else:
                                logger.info(
                                    "feature_spec_design_returned_empty",
                                    job_id=job_id,
                                )
                            enriched.append(a)
                        except Exception as exc:
                            logger.warning("feature_spec_design_failed", job_id=job_id, error=str(exc))
                            enriched.append(a)
                    return enriched

                enriched_actions = await _enrich_propose_features_with_specs(o, ml_ctx, stage0, translated)

                # Replace actions with enriched list for execution
                rr = review_result if isinstance(review_result, dict) else {}
                rr["next_actions"] = enriched_actions
                await _execute_ml_actions(rr, job_id, prev_oof_metrics, cur_oof_metrics)

            except Exception as e:
                logger.error("ml_actions_execution_failed", job_id=job_id, error=str(e), exc_info=True)
                # Write to DLQ but don't crash consumer
                try:
                    _send_to_dlq(
                        {"event": "ml.job", "status": "completed", "job_id": job_id, "job_type": job_type},
                        str(e),
                    )
                except Exception:
                    pass

            # Invalidate propose_plan caches to refresh next cycle (best-effort)
            cdir = _runs_dir() / dstr / "cache"
            try:
                cdir.mkdir(parents=True, exist_ok=True)
                (cdir / "propose_plan.invalidate").write_text(
                    json.dumps(
                        {
                            "ts": datetime.now().isoformat(),
                            "reason": "ml_completed",
                            "job_id": job_id,
                            "job_type": job_type,
                        }
                    ),
                    encoding="utf-8",
                )
                logger.info("plan_cache_invalidated", reason="ml_completed", job_id=job_id)
            except Exception:
                pass
        else:
            logger.warning("brain_review_ml_not_available", job_id=job_id)
            # Fallback: just log the completion
            review_file = _runs_dir() / dstr / "ml_review.json"
            review_file.parent.mkdir(parents=True, exist_ok=True)
            with review_file.open("w") as f:
                json.dump({"job_id": job_id, "job_type": job_type, "ml_ctx": ml_ctx, "review": "brain_unavailable"}, f, indent=2)
    except Exception as e:
        logger.error("brain_review_failed", job_id=job_id, error=str(e), exc_info=True)
        raise


async def _handle_ml_failed(evt: Dict[str, Any]) -> None:
    """Handle failed ML job event - record to ml_failed.jsonl for debugging."""
    dstr = date.today().isoformat()
    job_id = evt.get("job_id", "unknown")
    job_type = evt.get("job_type", "unknown")
    error_type = evt.get("error_type", "unknown")
    error_message = evt.get("error_message", "")

    logger.warning("ml_job_failed_received", job_id=job_id, job_type=job_type,
                  error_type=error_type, error_message=error_message[:200])

    # Append to ml_failed.jsonl for audit
    failed_log = _runs_dir() / dstr / "ml_failed.jsonl"
    failed_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with failed_log.open("a") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        logger.info("ml_failed_recorded", job_id=job_id, file=str(failed_log))
        # Invalidate plan cache so next loop reconsiders plan under failure context
        try:
            cdir = _runs_dir() / dstr / "cache"
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "propose_plan.invalidate").write_text(json.dumps({
                "ts": datetime.now().isoformat(),
                "reason": "ml_failed",
                "job_id": job_id,
                "job_type": job_type,
                "error_type": error_type
            }), encoding="utf-8")
            logger.info("plan_cache_invalidated", reason="ml_failed", job_id=job_id)
        except Exception:
            pass
    except Exception as e:
        logger.error("ml_failed_record_failed", job_id=job_id, error=str(e), exc_info=True)
        raise


async def _notify_brain_from_event_impl(evt: Dict[str, Any]) -> Dict[str, Any] | None:
    """Ask Brain to produce a fresh plan (afterhours flavor) and persist it.

    Keep this lightweight: best-effort, no hard failures; do not block consumer.
    """
    from scripts.ai_trader.orchestrator import Orchestrator  # local import to avoid heavy deps at module import
    try:
        o = Orchestrator()
        if not getattr(o, "brain_enabled", False):
            return {"status": "skipped", "reason": "brain_disabled"}
        # Compose a minimal context
        try:
            mem = {}
            from scripts.ai_trader.memory.aggregator import memory_summary
            mem = memory_summary("afterhours", lookback_days=14)
        except Exception:
            mem = {}
        ctx = {"health": o.health.check(), "memory": mem, "time": o._time_context(), "event": {"type": evt.get("type")}}
        # Call Brain with Stage 0 precheck and persist the plan (reuse internal helpers for consistency)
        # IMPORTANT: We're inside an async consumer; avoid asyncio.run() inside propose_plan.
        # Offload the sync call to a thread.
        try:
            import asyncio as _aio
            plan = await _aio.to_thread(o.propose_plan_stage0_and_call, "afterhours", ctx)
        except Exception:
            # Fallback for environments without to_thread (Py<3.9): run in default executor
            loop = _aio.get_running_loop()
            plan = await loop.run_in_executor(None, lambda: o.propose_plan_stage0_and_call("afterhours", ctx))
        try:
            # Persist and reconcile running tools against plan (best-effort)
            o._persist_plan("afterhours", plan)  # type: ignore[attr-defined]
            o._reconcile_processes_with_plan(plan)  # type: ignore[attr-defined]
        except Exception:
            pass
        return {"status": "ok", "persisted": True}
    except Exception as e:
        logger.warning("brain_notify_failed", err=str(e), exc_info=True)
        raise  # Re-raise for retry decorator


# Retry-wrapped version with exponential backoff
if TENACITY_AVAILABLE:
    _notify_brain_from_event = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        retry=retry_if_exception_type(Exception),
        reraise=True
    )(_notify_brain_from_event_impl)
else:
    _notify_brain_from_event = _notify_brain_from_event_impl


async def consume() -> None:
    try:
        from aiokafka import AIOKafkaConsumer
    except Exception:
        logger.warning("aiokafka_missing_or_unavailable")
        return

    # Resolve Kafka bootstrap and topic
    kc = _cfg.kafka_config

    # Subscribe to both control and ML topics
    topics = []
    try:
        tmap = _cfg.get("event_bus.topics", {}) or {}
        control_topic = tmap.get("control") or "internal.control"
        ml_topic = tmap.get("ml") or "internal.ml.status"
        topics = [control_topic, ml_topic]
    except Exception:
        topics = ["internal.control", "internal.ml.status"]

    group_id = os.getenv("CONTROL_CONSUMER_GROUP", "ai_trader_control_consumer")
    client_id = os.getenv("CONTROL_CONSUMER_CLIENT", "ai_trader_control_consumer")

    consumer = AIOKafkaConsumer(
        *topics,  # Subscribe to multiple topics
        bootstrap_servers=kc.get("bootstrap_servers") or ["127.0.0.1:9092"],
        group_id=group_id,
        client_id=client_id,
        enable_auto_commit=True,
        auto_offset_reset="earliest",  # Read all messages from beginning on first start or offset loss
        value_deserializer=lambda v: v,  # raw bytes
    )
    await consumer.start()
    logger.info("control_consumer_started", topics=topics, group=group_id)
    try:
        async for msg in consumer:
            start_time = time.time()
            evt_type = None
            status = "unknown"

            try:
                raw = msg.value.decode("utf-8") if isinstance(msg.value, (bytes, bytearray)) else str(msg.value)
                evt = json.loads(raw)
            except Exception:
                # Ignore non-JSON payloads
                if METRICS_AVAILABLE:
                    events_processed.labels(event_type="invalid_json", status="skipped").inc()
                continue

            # Handle ML events (new event format)
            if evt.get("event") == "ml.job":
                ml_status = evt.get("status")
                ml_job_type = evt.get("job_type")

                # Append to events file for audit
                try:
                    _append_event_line({"source": "kafka", "topic": msg.topic, **evt})
                except Exception:
                    pass

                if ml_status == "completed" and ml_job_type in {"oof", "feature_build", "feature_eval", "feature_apply", "ensemble_sweep", "alpha_loop", "hpo_cli"}:
                    # OOF completed - trigger Brain review
                    try:
                        await _handle_ml_completed(evt)
                        status = "processed"
                    except Exception as e:
                        status = "failed"
                        logger.error("ml_completed_handling_failed", job_id=evt.get("job_id"), error=str(e), exc_info=True)
                    if METRICS_AVAILABLE:
                        events_processed.labels(event_type=f"ml.job.completed.{ml_job_type}", status=status).inc()

                elif ml_status == "failed":
                    # ML job failed - record to ml_failed.jsonl
                    try:
                        await _handle_ml_failed(evt)
                        status = "recorded"
                    except Exception as e:
                        status = "failed"
                        logger.error("ml_failed_handling_failed", job_id=evt.get("job_id"), error=str(e), exc_info=True)
                    if METRICS_AVAILABLE:
                        events_processed.labels(event_type="ml.job.failed", status=status).inc()

                elif ml_status == "started":
                    # Just log started events for now
                    logger.info("ml_job_started", job_id=evt.get("job_id"), job_type=ml_job_type)
                    if METRICS_AVAILABLE:
                        events_processed.labels(event_type="ml.job.started", status="logged").inc()

                continue

            # Legacy event format (type field)
            evt_type = str(evt.get("type") or "").lower()

            # Support both OOF events and HPO/RL events
            if evt_type not in {"oof_complete", "oof_nopass", "hpo_complete", "rl_eval_complete"}:
                if METRICS_AVAILABLE:
                    events_processed.labels(event_type=evt_type, status="ignored").inc()
                continue

            # Append to events file for audit
            try:
                _append_event_line({"source": "kafka", "topic": msg.topic, **evt})
            except Exception:
                pass

            # Rate-limit Brain notifications
            if _should_rate_limit("on_oof_event"):
                status = "rate_limited"
                if METRICS_AVAILABLE:
                    events_processed.labels(event_type=evt_type, status=status).inc()
                continue

            # Notify Brain now with retry (low volume, acceptable to await directly)
            try:
                result = await _notify_brain_from_event(evt)
                status = result.get("status", "ok") if result else "ok"
                if METRICS_AVAILABLE:
                    events_processed.labels(event_type=evt_type, status=status).inc()
            except Exception as e:
                status = "failed"
                error_msg = str(e)
                logger.error("event_processing_failed", event_type=evt_type, err=error_msg, exc_info=True)
                _send_to_dlq(evt, error_msg)
                if METRICS_AVAILABLE:
                    events_processed.labels(event_type=evt_type, status=status).inc()
            finally:
                # Track processing duration
                if METRICS_AVAILABLE and evt_type:
                    duration = time.time() - start_time
                    event_processing_duration.labels(event_type=evt_type).observe(duration)
    finally:
        await consumer.stop()


def _acquire_singleton_lock() -> bool:
    """Acquire singleton lock with stale lock cleanup.

    Checks if a control_event_consumer process is actually running.
    If lock exists but process is dead, removes stale lock and proceeds.

    Returns:
        True if lock acquired, False if another instance is running
    """
    try:
        dstr = date.today().isoformat()
        p = _runs_dir() / dstr / "locks" / "control_consumer.lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()

        if p.exists() and (now - p.stat().st_mtime) < 12 * 3600:
            # Lock exists and is fresh - check if process is actually running
            try:
                import subprocess
                result = subprocess.run(
                    ["pgrep", "-f", "control_event_consumer.py"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Process is running - respect the lock
                    logger.info("control_consumer_already_running", lock_age_sec=int(now - p.stat().st_mtime))
                    return False
                else:
                    # Lock exists but no process - stale lock, clean it up
                    logger.warning("stale_lock_detected", lock_age_sec=int(now - p.stat().st_mtime))
                    p.unlink()
            except Exception as e:
                # If we can't check process, be conservative and respect the lock
                logger.warning("process_check_failed", error=str(e))
                return False

        # No lock or stale lock cleaned up - acquire it
        p.write_text(str(int(now)), encoding="utf-8")
        logger.info("singleton_lock_acquired", lock_file=str(p))
        return True
    except Exception as e:
        logger.warning("lock_acquisition_error", error=str(e))
        return True  # Fail open to allow startup


def _ttl_lock(tag: str, ttl_sec: int = 900) -> bool:
    """Check if action is locked by TTL.

    Returns:
        True if locked (should skip), False if available (can execute)
    """
    try:
        locks_dir = _runs_dir() / date.today().isoformat() / "locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / f"{tag}.lock"

        if lock_file.exists():
            age = time.time() - lock_file.stat().st_mtime
            if age < ttl_sec:
                return True  # Still locked

        # Acquire lock
        lock_file.write_text(str(int(time.time())), encoding="utf-8")
        return False  # Lock acquired, can proceed
    except Exception:
        return False  # On error, allow execution


# (moved earlier) duplicate optimization helpers removed


def _get_strategy_symbols() -> list[str]:
    """Get current strategy symbols from config or universe."""
    try:
        from scripts.ai_trader.orchestrator import Orchestrator
        o = Orchestrator()
        # Try to get symbols from config
        if hasattr(o, '_strategy_symbols_from_config'):
            return o._strategy_symbols_from_config()
        # Fallback to universe
        universe = _cfg.get("universe", [])
        return [str(s) for s in universe] if universe else []
    except Exception as e:
        logger.warning("failed_to_get_symbols", error=str(e))
        return []


async def _execute_backfill(params: dict, job_id: str) -> dict:
    """Execute backfill command directly using load_hist_multi.py for synchronous data loading.

    Args:
        params: Action parameters (may contain 'symbols', 'days', 'bar_size')
        job_id: ML job ID for audit

    Returns:
        Execution result with status, stdout, stderr
    """
    result = {
        "status": "failed",
        "stdout": "",
        "stderr": "",
        "command": ""
    }

    try:
        # Parse symbols
        symbols_param = params.get("symbols", [])
        if isinstance(symbols_param, str):
            symbols = [s.strip() for s in symbols_param.split(",") if s.strip()]
        elif isinstance(symbols_param, list):
            symbols = [str(s) for s in symbols_param]
        else:
            symbols = []

        # Fallback to config symbols if not specified
        if not symbols:
            symbols = _get_strategy_symbols()

        if not symbols:
            result["stderr"] = "No symbols available for backfill"
            return result

        # Optional: focus backfill on symbols with low *historical* coverage
        # rather than thrashing on the current intraday session.
        try:
            # Align date logic with _execute_data_quality_check: use T-1
            # calendar day as the coverage check target, and let
            # check_active_completeness auto-seed the active table from the
            # baseline features table when needed.
            from datetime import date as _date, timedelta as _td

            raw_day = _date.today()
            check_day = (raw_day - _td(days=1)).isoformat()

            helper_cmd = [
                sys.executable,
                "tools/check_active_completeness.py",
                "--date",
                check_day,
                "--symbols",
                ",".join(symbols),
                "--min-coverage",
                "0.98",
                "--auto-seed-active",
            ]
            proc_cov = subprocess.run(
                helper_cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=os.getcwd(),
            )
            low_cov_syms: List[str] = []
            if proc_cov.stdout:
                try:
                    payload = json.loads(proc_cov.stdout)
                    for rec in payload.get("symbols", []):
                        cov = float(rec.get("coverage", 0.0) or 0.0)
                        sym = str(rec.get("symbol") or "").strip()
                        if sym and cov < 0.98:
                            low_cov_syms.append(sym)
                except Exception:
                    low_cov_syms = []
            if low_cov_syms:
                logger.info(
                    "backfill_symbols_filtered_by_coverage",
                    original=symbols,
                    filtered=low_cov_syms,
                    job_id=job_id,
                )
                symbols = low_cov_syms
            else:
                # 所有标的在历史窗口内覆盖率都已达标，不需要再触发回补。
                logger.info(
                    "backfill_skipped_no_low_coverage",
                    symbols=symbols,
                    check_date=check_day,
                    job_id=job_id,
                )
                result["status"] = "skipped"
                result["command"] = ""
                return result
        except Exception as e:
            logger.debug("backfill_symbol_coverage_scan_failed", error=str(e), job_id=job_id)

        # Build command
        # Default to a longer backfill window to avoid repeated shallow retries
        days = int(params.get("days", 60) or 60)
        min_days_floor = int(params.get("min_days_for_backfill", 30) or 30)
        if days < min_days_floor:
            logger.info("backfill_days_floored", requested=days, min_days=min_days_floor, job_id=job_id)
            days = min_days_floor
        bar_size = str(params.get("bar_size", "1m"))
        chunk_days = int(params.get("chunk_days", 3) or 3)
        symbol_batch_size = int(params.get("symbol_batch_size", 3) or 3)  # default smaller batches for reliability
        # Allow longer timeout for larger universes; Brain can override via slice_timeout_sec in params.
        slice_timeout_sec = int(params.get("slice_timeout_sec", 3600) or 3600)
        force_full_window = bool(params.get("force_full_window", False))

        cmds: List[List[str]] = []
        # Symbol batching to avoid oversized requests (e.g., IBKR limits)
        def _batches(seq: List[str], n: int) -> List[List[str]]:
            return [seq[i:i + n] for i in range(0, len(seq), n)] if n > 0 else [seq]

        symbol_batches = _batches(symbols, symbol_batch_size)

        # For 1m bars, IBKR is sensitive to long duration; clamp chunk_days to 1 for safety
        try:
            tf_norm = bar_size.strip().lower().replace(" ", "")
            if tf_norm in {"1m", "1min", "1minute"} and chunk_days > 1:
                logger.info(
                    "backfill_chunk_days_clamped_for_1m",
                    requested=chunk_days,
                    effective=1,
                    job_id=job_id,
                )
                chunk_days = 1
        except Exception:
            pass

        for sym_batch in symbol_batches:
            cmd = [
                sys.executable,
                "tool/load_hist_multi.py",
                "--symbols"
            ] + sym_batch + [
                "--days", str(days),
                "--bar-size", bar_size,
                "--fill-mode", "backfill",
                "--chunk-days", str(chunk_days),
                "--skip-covered-days",
            ]
            cmds.append(cmd)

        # === [FIX START: Force Direct Sync Backfill for each slice] ===

        # Execute each slice with timeout
        stdout_all: List[str] = []
        stderr_all: List[str] = []
        rc_overall = 0
        for cmd in cmds:
            result["command"] = " ".join(cmd)
            logger.info("executing_backfill_direct", command=result["command"], job_id=job_id, symbols=symbols)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=slice_timeout_sec, cwd=os.getcwd())
            stdout_all.append(proc.stdout[-400:] if proc.stdout else "")
            stderr_all.append(proc.stderr[-400:] if proc.stderr else "")
            if proc.returncode != 0:
                rc_overall = proc.returncode
                
                
        result["stdout"] = "\n".join(stdout_all)[-1000:]
        result["stderr"] = "\n".join(stderr_all)[-1000:]
        result["returncode"] = rc_overall

        if rc_overall == 0:
            result["status"] = "ok"
            logger.info("backfill_executed_successfully", job_id=job_id, symbols=symbols, mode="direct_sync")

            # Post-refresh: trigger feature recompute and data quality check inline
            # so Brain quickly sees updated coverage and features without scheduler
            try:
                fr_params = {"symbols": symbols}
                fr_res = await _execute_feature_recompute(fr_params, job_id)
                logger.info(
                    "backfill_post_feature_recompute",
                    job_id=job_id,
                    status=fr_res.get("status"),
                )
            except Exception as e:
                logger.warning("backfill_post_feature_recompute_failed", job_id=job_id, error=str(e))

            # Small delay to allow feature recompute outputs to become visible
            await asyncio.sleep(2.0)

            try:
                dq_params = {"symbols": symbols}
                dq_res = await _execute_data_quality_check(dq_params, job_id)
                logger.info(
                    "backfill_post_data_quality_check",
                    job_id=job_id,
                    status=dq_res.get("status"),
                )
            except Exception as e:
                logger.warning("backfill_post_data_quality_failed", job_id=job_id, error=str(e))
        else:
            result["status"] = "failed"
            logger.error("backfill_direct_sync_failed", job_id=job_id, rc=proc.returncode, stderr=result["stderr"])

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["stderr"] = f"Backfill execution timeout ({slice_timeout_sec}s)"
        logger.error("backfill_timeout", job_id=job_id, timeout_sec=slice_timeout_sec)
    except Exception as e:
        result["status"] = "error"
        result["stderr"] = str(e)
        logger.error("backfill_execution_error", job_id=job_id, error=str(e), exc_info=True)

    return result


async def _enqueue_ml_job(params: dict, job_id: str) -> dict:
    """Enqueue ML job to scheduler queue.

    Args:
        params: Action parameters (should contain 'job_type', 'priority', 'params')
        job_id: ML job ID for audit

    Returns:
        Execution result
    """
    result = {
        "status": "ok",
        "queued_job_id": None
    }

    try:
        job_type = params.get("job_type")
        if not job_type:
            result["status"] = "skipped"
            result["reason"] = "No job_type in params"
            return result

        # Whitelist check (P2.3: Added feature_build, hpo_cli for ML diagnostics)
        # Note: feature_recompute/data_quality_check are executed inline in control_consumer,
        # not as standalone scheduler jobs.
        allowed_types = [
            "oof",
            "alpha_loop",
            "lstm",
            "ensemble_sweep",
            "feature_build",
            "feature_eval",
            "feature_apply",
            "feature_search",
            "feature_resample",
            "feature_experiment",
            "label_diagnostics",
            "label_recompute",
            "feature_promote",
            "feature_ablation",
            "hpo_cli",
            "strategy_experiment",
            "bulk_backfill",
            "feature_enhanced_backfill",
        ]
        if job_type not in allowed_types:
            result["status"] = "rejected"
            result["reason"] = f"job_type={job_type} not in whitelist {allowed_types}"
            logger.warning("ml_job_type_rejected", job_type=job_type, job_id=job_id)
            return result

        # Simple cooldown for enhanced backfill to avoid thrashing: only allow
        # one feature_enhanced_backfill enqueue per cooldown window.
        if job_type == "feature_enhanced_backfill":
            from datetime import datetime, timedelta
            cooldown_min = int(params.get("cooldown_min", 30) or 30)
            lock_dir = _runs_dir() / date.today().isoformat() / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_file = lock_dir / "feature_enhanced_backfill.lock"
            now = datetime.now()
            try:
                if lock_file.exists():
                    ts = datetime.fromisoformat(lock_file.read_text(encoding="utf-8").strip())
                    if (now - ts) < timedelta(minutes=cooldown_min):
                        result["status"] = "rejected"
                        result["reason"] = f"cooldown_active:{cooldown_min}min"
                        logger.warning(
                            "feature_enhanced_backfill_rejected_cooldown",
                            job_id=job_id,
                            cooldown_min=cooldown_min,
                        )
                        return result
                # update lock timestamp
                lock_file.write_text(now.isoformat(), encoding="utf-8")
            except Exception as e:
                logger.warning("feature_enhanced_backfill_cooldown_check_failed", error=str(e), job_id=job_id)

        # Enqueue job via scheduler
        from scripts.ai_trader.ml_scheduler import enqueue_job

        job_params = params.get("params", {})
        priority = params.get("priority")  # None = use default

        queued_job_id = enqueue_job({
            "job_type": job_type,
            "priority": priority,
            "params": job_params,
            "source_job_id": job_id,  # Link to brain review job
        })

        result["queued_job_id"] = queued_job_id
        logger.info("ml_job_enqueued", job_type=job_type, queued_job_id=queued_job_id, source_job_id=job_id)

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.error("ml_job_enqueue_failed", job_id=job_id, error=str(e), exc_info=True)

    return result


async def _record_extend_window_hint(params: dict, job_id: str) -> dict:
    """Record extend_window hint to hpo_hints.json and auto-trigger OOF (P1 feature).

    Args:
        params: Action parameters (should contain 'window_days')
        job_id: ML job ID for audit

    Returns:
        Execution result
    """
    result = {
        "status": "ok",
        "hint_recorded": False,
        "auto_triggered": False
    }

    try:
        window_days = params.get("window_days")
        if not window_days:
            result["status"] = "skipped"
            result["reason"] = "No window_days in params"
            return result

        # Whitelist check (updated to match Brain's options: 30,60,90,120,150,180)
        allowed_windows = [30, 60, 90, 120, 150, 180]
        if window_days not in allowed_windows:
            result["status"] = "rejected"
            result["reason"] = f"window_days={window_days} not in whitelist {allowed_windows}"
            logger.warning("extend_window_rejected", window_days=window_days, job_id=job_id)
            return result

        # Record to hpo_hints.json
        hints_file = _runs_dir() / date.today().isoformat() / "hpo_hints.json"
        hints_file.parent.mkdir(parents=True, exist_ok=True)

        # Merge with existing hints
        hints = {}
        if hints_file.exists():
            hints = json.loads(hints_file.read_text("utf-8"))

        hints["window_days"] = window_days
        hints["ts"] = datetime.now().isoformat()
        hints["job_id"] = job_id
        hints["source"] = "brain_extend_window"

        hints_file.write_text(json.dumps(hints, indent=2), encoding="utf-8")
        result["hint_recorded"] = True
        logger.info("extend_window_hint_recorded", window_days=window_days, job_id=job_id)

        # Auto-trigger OOF evaluation with new window size
        symbols = params.get("symbols", ["TSLA"])
        oof_params = {
            "job_type": "oof",
            "priority": "high",
            "params": {
                "symbols": symbols,
                "window_days": window_days,
            }
        }
        trigger_result = await _enqueue_ml_job(oof_params, job_id)
        if trigger_result.get("status") == "ok":
            result["auto_triggered"] = True
            result["queued_job_id"] = trigger_result.get("queued_job_id")
            logger.info("extend_window_auto_triggered_oof",
                       window_days=window_days,
                       symbols=symbols,
                       queued_job_id=result["queued_job_id"])
        else:
            logger.warning("extend_window_auto_trigger_failed",
                          trigger_result=trigger_result)

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.error("extend_window_hint_failed", job_id=job_id, error=str(e), exc_info=True)

    return result


async def _record_reduce_window_hint(params: dict, job_id: str) -> dict:
    """Record reduce_window hint to hpo_hints.json with safe set {30,60} and auto-trigger OOF."""
    result = {"status": "ok", "hint_recorded": False, "auto_triggered": False}
    try:
        window_days = params.get("window_days")
        if not window_days:
            result["status"] = "skipped"
            result["reason"] = "No window_days in params"
            return result
        try:
            window_days = int(window_days)
        except Exception:
            result["status"] = "rejected"
            result["reason"] = "window_days_not_int"
            return result
        allowed = {30, 60}
        if window_days not in allowed:
            # Normalize to nearest allowed
            window_days = 60 if window_days > 45 else 30
        hints_file = _runs_dir() / date.today().isoformat() / "hpo_hints.json"
        hints_file.parent.mkdir(parents=True, exist_ok=True)
        hints = {}
        if hints_file.exists():
            hints = json.loads(hints_file.read_text("utf-8"))
        hints.update({
            "window_days": window_days,
            "ts": datetime.now().isoformat(),
            "job_id": job_id,
            "source": "brain_reduce_window",
        })
        hints_file.write_text(json.dumps(hints, indent=2), encoding="utf-8")
        logger.info("reduce_window_hint_recorded", window_days=window_days, job_id=job_id)
        result["hint_recorded"] = True

        # Auto-trigger OOF evaluation with new window size
        symbols = params.get("symbols", ["TSLA"])
        oof_params = {
            "job_type": "oof",
            "priority": "high",
            "params": {
                "symbols": symbols,
                "window_days": window_days,
            }
        }
        trigger_result = await _enqueue_ml_job(oof_params, job_id)
        if trigger_result.get("status") == "ok":
            result["auto_triggered"] = True
            result["queued_job_id"] = trigger_result.get("queued_job_id")
            logger.info("reduce_window_auto_triggered_oof",
                       window_days=window_days,
                       symbols=symbols,
                       queued_job_id=result["queued_job_id"])
        else:
            logger.warning("reduce_window_auto_trigger_failed",
                          trigger_result=trigger_result)

        return result
    except Exception as e:
        logger.error("reduce_window_hint_failed", job_id=job_id, error=str(e), exc_info=True)
        return {"status": "error", "reason": str(e)}


async def _execute_data_quality_check(params: dict, job_id: str) -> dict:
    """Run system/data quality checks (best-effort), return status + rc.

    For non-trading days (weekends), skip active coverage enforcement and treat
    the check as OK to avoid false alarms and backfill loops.
    """
    try:
        import subprocess as _sp
        from datetime import date as _date, timedelta as _td
        import datetime as _dt
        from zoneinfo import ZoneInfo
        # 1) System health (fast)
        cmd_health = ["venv/bin/python", "docs/daily/system_health_check.py", "--json"]
        p1 = _sp.run(cmd_health, text=True, capture_output=True, timeout=180)
        # 2) Active table completeness (slow)
        raw_day = str(params.get("date") or _date.today().isoformat())

        # For ML/data gating we intentionally check the *previous* calendar day
        # rather than "today", because:
        #   - intraday 1m coverage is often incomplete before session end, and
        #   - IBKR historical APIs may not return same-day bars yet.
        # Historical ML workflows only require stable T-1 data quality.
        try:
            _day_dt = _dt.date.fromisoformat(raw_day)
            _check_dt = _day_dt - _td(days=1)
            day = _check_dt.isoformat()
        except Exception:
            day = raw_day

        # Skip coverage enforcement on weekends (no regular session expected)
        try:
            day_dt = _dt.date.fromisoformat(day)
            # Monday=0, Sunday=6; skip Sat/Sun
            if day_dt.weekday() >= 5:
                return {
                    "status": "ok",
                    "health_rc": p1.returncode,
                    "active_rc": 0,
                    "report": None,
                    "reason": "weekend_no_rth_session",
                }
        except Exception:
            pass

        symbols = params.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        if not symbols:
            symbols = _get_strategy_symbols()
        min_cov = float(params.get("min_coverage", 0.98))
        cmd_active = [
            sys.executable,
            "tools/check_active_completeness.py",
            "--date", day,
            "--symbols", ",".join(symbols),
            "--min-coverage", str(min_cov),
            "--auto-seed-active",
        ]
        p2 = _sp.run(cmd_active, text=True, capture_output=True, timeout=600)
        # Persist active coverage report
        try:
            d = _runs_dir() / _date.today().isoformat()
            d.mkdir(parents=True, exist_ok=True)
            rep = d / "data_quality.json"
            payload = {
                "ts": datetime.now().isoformat(),
                "job_id": job_id,
                "health_rc": p1.returncode,
                "health_tail": p1.stdout[-400:] if p1.stdout else "",
                "active_rc": p2.returncode,
                "active_cmd": " ".join(cmd_active),
                "active_report": None,
            }
            try:
                payload["active_report"] = json.loads(p2.stdout) if p2.stdout else None
            except Exception:
                payload["active_report"] = {"raw": (p2.stdout[-800:] if p2.stdout else "")}
            rep.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            report_path = str(rep)
        except Exception:
            report_path = None
        # For ML/Brain gating we treat active coverage result (p2) as the primary signal:
        # - p2 returncode in (0,1) means the check executed successfully (1 = below threshold).
        # - system_health (p1) is advisory; its non-zero returncode should not force hard failure
        #   of data_quality_check in ML loops, otherwise missing Docker/ticks will block all ML.
        # We still persist health_rc so Brain/ops can inspect it.
        status = "ok" if p2.returncode in (0, 1) else "failed"
        # Note: check_active_completeness exits 1 when coverage < threshold; that is still a
        # successful execution and is captured via active_report, not via status.
        return {
            "status": status,
            "health_rc": p1.returncode,
            "active_rc": p2.returncode,
            "report": report_path,
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def _execute_feature_recompute(params: dict, job_id: str) -> dict:
    """Recompute features for a symbol list and date window (1m resolution default)."""
    result = {"status": "failed"}
    try:
        import subprocess as _sp
        from datetime import date as _date, timedelta
        symbols = params.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        if not symbols:
            symbols = _get_strategy_symbols()
        if not symbols:
            return {"status": "skipped", "reason": "no_symbols"}
        start = params.get("start")
        end = params.get("end")
        if not start or not end:
            # Default to yesterday and today
            today = _date.today()
            start = (today - timedelta(days=1)).isoformat()
            end = today.isoformat()
        ok_all = True
        details = []
        for sym in symbols:
            cmd = [
                sys.executable,
                "services/market/features/feature_calculator.py",
                "--symbol", sym,
                "--start", start,
                "--end", end,
                "--resolution", "1m",
            ]
            # Set PYTHONPATH for the subprocess
            env = os.environ.copy()
            env["PYTHONPATH"] = str(_PROJECT_ROOT) # Use the already defined _PROJECT_ROOT
            p = _sp.run(cmd, text=True, capture_output=True, timeout=900, env=env, cwd=_PROJECT_ROOT)
            details.append({"symbol": sym, "rc": p.returncode, "stderr_tail": (p.stderr[-400:] if p.stderr else "")})
            ok_all = ok_all and (p.returncode == 0)
        result = {"status": ("ok" if ok_all else "failed"), "details": details, "window": {"start": start, "end": end}}
        return result
    except Exception as e:
        return {"status": "error", "reason": str(e)}


FEATURE_CATALOG_ALLOWED_SOURCES = {"m5", "d"}


def _write_features_catalog(params: dict) -> tuple[bool, str, list[str]]:
    """Write proposed features into config/features_catalog.json as candidates.

    Params expected:
      - version: str
      - specs: list of {name, expr/sql, ...}
    """
    try:
        version = str(params.get("version") or date.today().isoformat())
        specs = params.get("specs") or []
        if not isinstance(specs, list) or not specs:
            return False, "no_specs", []
        p = Path("config/features_catalog.json")
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text("utf-8"))
            except Exception:
                data = {}
        data.setdefault("active", [])
        data.setdefault("candidate", [])
        # Merge by name (replace existing candidate with same name)
        names = {c.get("name") for c in data["candidate"] if isinstance(c, dict)}
        new_list = [c for c in data["candidate"] if isinstance(c, dict) and c.get("name") not in {s.get("name") for s in specs if isinstance(s, dict)}]
        used_sources: Set[str] = set()
        accepted_count = 0
        unknown_dsl_ops: Set[str] = set()
        # Optional DSL→SQL converter for structured feature specs
        try:
            from libs.features.feature_dsl_sql import dsl_to_sql  # type: ignore
        except Exception:
            dsl_to_sql = None  # type: ignore[assignment]
        # Filter out specs that rely on unsupported SQL-style constructs or unsafe midprice/orderbook aliases.
        # Brain is expected to migrate toward ClickHouse-compatible expressions or DSL-based features.
        banned_fragments = (
            "lag(",
            "mean(",
            "stddev(",
            "runningdifference(",
            "linearregressionslope(",
            "neighbor(",
            "bidask_midpoint",
            "bid_price",
            "ask_price",
            "ask_size",
            "bid_size",
        )

        def _dsl_has_null(node: Any) -> bool:
            """Recursively detect None in DSL spec to prevent NoneType errors.

            The SQL DSL expects every operand (num/den/col/col1/col2/...) to be
            a valid column string or nested DSL object. If Brain emits null/None
            for any of these, we treat the spec as invalid and skip it so that
            feature_dsl_sql does not raise Unsupported DSL node type: NoneType.
            """
            if node is None:
                return True
            if isinstance(node, dict):
                for v in node.values():
                    if _dsl_has_null(v):
                        return True
            elif isinstance(node, (list, tuple)):
                for v in node:
                    if _dsl_has_null(v):
                        return True
            return False

        for s in specs:
            if isinstance(s, dict) and s.get("name"):
                # For Brain‑proposed specs we only accept DSL‑based definitions.
                # Raw SQL expr/sql/expr_sql from LLM is disallowed here to avoid
                # fragile ClickHouse syntax; established hand‑written features
                # live under the 'active' catalog and are not affected.
                if not s.get("dsl") and (s.get("expr") or s.get("sql") or s.get("expr_sql")):
                    logger.warning(
                        "feature_spec_expr_disallowed",
                        name=s.get("name"),
                    )
                    continue
                # If Brain provided a DSL spec, ALWAYS convert it into a
                # ClickHouse expression and ignore any raw expr/sql the LLM
                # may have attached. This keeps the catalog format stable and
                # prevents malformed expr placeholders like '(op) * (col)'
                # from slipping through.
                if dsl_to_sql is not None and s.get("dsl"):
                    try:
                        # Fast-fail on any null/None in the DSL tree to avoid
                        # NoneType compile errors; Brain is instructed not to
                        # use null/None for operands, so such specs are treated
                        # as invalid and skipped.
                        if _dsl_has_null(s["dsl"]):
                            logger.warning(
                                "feature_spec_dsl_has_null",
                                name=s.get("name"),
                                dsl=s.get("dsl"),
                            )
                            continue
                        expr_from_dsl = dsl_to_sql(s["dsl"])
                        s["expr"] = expr_from_dsl
                        # Drop any stale expr/sql/expr_sql fields provided by
                        # the LLM so downstream logic sees only the compiled
                        # expression.
                        s.pop("sql", None)
                        s.pop("expr_sql", None)
                    except Exception as exc:
                        msg = str(exc)
                        logger.warning(
                            "feature_spec_dsl_compile_failed",
                            name=s.get("name"),
                            error=msg,
                        )
                        # Best-effort extraction of unsupported op name for diagnostics
                        if "Unknown DSL op:" in msg:
                            try:
                                    op = msg.split("Unknown DSL op:", 1)[1].strip().split()[0].strip(":, ")
                                    if op:
                                        unknown_dsl_ops.add(op)
                            except Exception:
                                pass
                        # Skip this spec; it cannot be compiled from DSL and
                        # we intentionally do not trust any raw expr/sql.
                        continue

                expr_raw = str(s.get("expr") or s.get("sql") or s.get("expr_sql") or "")
                expr_lower = expr_raw.lower()
                # Hard-filter specs that reference columns known to be structurally
                # NULL or explicitly disabled for the current intraday setup.
                # Static blacklist (permanent bans only; dynamic bans are driven
                # by feature_status*.json below).
                forbidden_cols = [
                    # Microstructure (require tick data)
                    "tick_imbalance",
                    "trade_imb_5m",
                    "price_impact_5m",
                    "price_momentum_5m",
                    # Experimental features (not implemented)
                    "bb_position",
                    "bb_width",
                    "ema_cross_1m",
                    "ema_cross_5m",
                    "entropy_20",
                    "hurst_20",
                ]
                # Optional runtime blacklists from feature status configs:
                # any column with status starting with "disabled" will be treated
                # as forbidden for Brain-proposed specs. We merge both the static
                # config/feature_status.json and any runtime status file written
                # by data/coverage tools (e.g., runs/ai_trader/<date>/feature_status.runtime.json).
                try:
                    from pathlib import Path as _P
                    def _load_status(path: str) -> dict:
                        p = _P(path)
                        if not p.exists():
                            return {}
                        try:
                            return json.loads(p.read_text("utf-8"))
                        except Exception:
                            return {}

                    static_status = _load_status("config/feature_status.json")
                    # Runtime status lives under today's run directory if present.
                    dstr = datetime.now().date().isoformat()
                    runtime_status = _load_status(f"runs/ai_trader/{dstr}/feature_status.runtime.json")

                    for status in (static_status, runtime_status):
                        for cname, st in (status.get("columns") or {}).items():
                            try:
                                if isinstance(st, str) and st.strip().lower().startswith("disabled"):
                                    if cname not in forbidden_cols:
                                        forbidden_cols.append(cname)
                            except Exception:
                                continue
                except Exception:
                    pass
                if any(f".{col}" in expr_lower for col in forbidden_cols):
                    logger.warning(
                        "feature_spec_uses_forbidden_column",
                        name=s.get("name"),
                        expr=expr_raw[:80],
                    )
                    continue
                if any(bad in expr_lower for bad in banned_fragments):
                    # Skip unsupported spec; feature_build will not see it
                    continue
                if "required_sources" in s:
                    try:
                        rs = s.get("required_sources") or []
                        if isinstance(rs, list):
                            filt = sorted({str(item) for item in rs if str(item) in FEATURE_CATALOG_ALLOWED_SOURCES})
                            if filt:
                                s["required_sources"] = filt
                                used_sources.update(filt)
                            else:
                                s.pop("required_sources", None)
                        else:
                            s.pop("required_sources", None)
                    except Exception:
                        s.pop("required_sources", None)
                else:
                    # Autodetect from expression for the return value (sanitization handled in feature_build)
                    expr_lower = str(s.get("expr") or "").lower()
                    for alias in FEATURE_CATALOG_ALLOWED_SOURCES:
                        if f"{alias}." in expr_lower:
                            used_sources.add(alias)
                # Optional: best-effort dry-run of the expression against ClickHouse
                # to surface syntax/identifier issues early and filter out
                # degenerate specs that collapse to NULL/constant values on
                # real data. If the error is a schema-level problem (e.g.
                # alias/column not found), drop this spec so it does not break
                # feature_build; other errors (like transient connection issues)
                # are logged but do not block.
                try:
                    from scripts.ai_trader.brain.tools_sql import dry_run_feature_expr as _dry_run  # type: ignore
                    # Prefer explicitly declared required_sources when present
                    srcs = list(s.get("required_sources") or [])
                    out = _dry_run(expr_raw, sources=srcs or list(used_sources), save=True)
                    if isinstance(out, dict) and not out.get("ok", False):
                        err_type = str(out.get("error_type") or "")
                        # SchemaValidationError means the expression references
                        # columns that are not present in the underlying tables.
                        if err_type == "SchemaValidationError":
                            logger.warning(
                                "feature_spec_dry_run_schema_invalid",
                                name=s.get("name"),
                                error=out.get("error_message", ""),
                            )
                            # Skip invalid spec; do not let it into the catalog.
                            continue
                    # If dry-run succeeded, check value quality: drop specs that
                    # are entirely NULL or effectively constant on sample data.
                    if isinstance(out, dict) and out.get("ok", False):
                        rows_preview = out.get("rows_preview") or []
                        vals = []
                        for r in rows_preview:
                            if isinstance(r, dict) and "value" in r:
                                vals.append(r.get("value"))
                        non_null = [v for v in vals if v is not None]
                        # No non‑null samples → feature cannot train on real data.
                        if not non_null:
                            logger.warning(
                                "feature_spec_dry_run_all_null",
                                name=s.get("name"),
                                expr=expr_raw[:80],
                            )
                            continue
                        # If all non‑null samples have the same value, treat as
                        # constant and skip to avoid wasting model capacity.
                        distinct = {v for v in non_null}
                        if len(distinct) == 1 and len(non_null) >= 5:
                            logger.warning(
                                "feature_spec_dry_run_constant",
                                name=s.get("name"),
                                expr=expr_raw[:80],
                                const_value=list(distinct)[0],
                            )
                            continue
                except Exception as exc:
                    logger.warning("feature_spec_dry_run_failed", name=s.get("name"), error=str(exc))
                new_list.append(s)
                accepted_count += 1
        data["candidate"] = new_list
        data["version"] = version
        data["updated_at"] = datetime.now().isoformat()
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # If nothing was accepted, surface a clear failure reason so Brain
        # can adjust its DSL/expr usage instead of thinking the proposal
        # succeeded while all specs were dropped.
        if accepted_count == 0:
            if unknown_dsl_ops:
                reason = "unsupported_dsl_ops:" + ",".join(sorted(unknown_dsl_ops))
            else:
                reason = "no_valid_specs"
            return False, reason, []
        return True, "ok", sorted(used_sources)
    except Exception as e:
        return False, str(e), []


def _ensure_resample_sources(required_sources: list[str]) -> None:
    """Regenerate resampled feature tables when multi-source specs are proposed."""
    if not required_sources:
        return
    
    # Define a date range for chunked processing to avoid memory issues.
    end_date = date.today()
    start_date = end_date - timedelta(days=90)
    
    alias_cmds = {
        "m5": [
            sys.executable,
            "tools/feature_resample.py",
            "--source",
            "trading.features_1m",
            "--target",
            "trading.features_5m",
            "--kind",
            "five_min",
            "--interval",
            "5",
            "--replace",
            "--start-date",         # <-- 新增
            start_date.isoformat(), # <-- 新增
            "--end-date",           # <-- 新增
            end_date.isoformat(),   # <-- 新增
        ],
        "d": [
            sys.executable,
            "tools/feature_resample.py",
            "--source",
            "trading.features_1m",
            "--target",
            "trading.features_1d_prev",
            "--kind",
            "daily_prev",
            "--replace",
            "--start-date",         # <-- 新增
            start_date.isoformat(), # <-- 新增
            "--end-date",           # <-- 新增
            end_date.isoformat(),   # <-- 新增
        ],
    }
    for alias in sorted(set(required_sources)):
        cmd = alias_cmds.get(alias)
        if not cmd:
            continue
        try:
            subprocess.run(cmd, check=True, text=True, capture_output=True, cwd=_PROJECT_ROOT)
            logger.info("resample_source_ready", alias=alias, cmd=" ".join(cmd))
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "resample_source_failed",
                alias=alias,
                returncode=exc.returncode,
                stderr=(exc.stderr[-400:] if exc.stderr else ""),
            )
        except Exception as exc:
            logger.warning("resample_source_failed", alias=alias, error=str(exc))


def _infer_symbol_dependencies_from_specs(specs: list[dict]) -> list[str]:
    """Infer symbol-level data dependencies from Brain-proposed feature specs.

    This scans DSL/expr fields for cross-asset prefixes like f.spy_ret_5 or
    m5.qqq_ret_5 and maps them to concrete symbols drawn from the current
    strategy universe. It is intentionally conservative: if nothing matches,
    it returns an empty list; if multiple matches occur, all are returned.
    """
    try:
        universe = {s.lower(): s for s in _get_strategy_symbols()}
    except Exception:
        universe = {}
    if not universe:
        return []

    required: set[str] = set()

    def _spec_text(spec: dict) -> str:
        parts: list[str] = []
        for key in ("expr", "sql", "expr_sql"):
            v = spec.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
        dsl = spec.get("dsl")
        if isinstance(dsl, (dict, list)):
            try:
                parts.append(json.dumps(dsl, sort_keys=True))
            except Exception:
                pass
        return " ".join(parts).lower()

    for spec in specs:
        if not isinstance(spec, dict):
            continue
        text = _spec_text(spec)
        if not text:
            continue
        for sym_lower, sym in universe.items():
            # Look for patterns like f.spy_ret_5 / m5.spy_ret_5 / d.spy_ret_5
            token = f"{sym_lower}_"
            if (
                f"f.{token}" in text
                or f"m5.{token}" in text
                or f"d.{token}" in text
            ):
                required.add(sym)

    return sorted(required)


def _apply_hpo_params_from_artifacts(evt: Dict[str, Any]) -> None:
    """Best-effort: derive best_params JSON and apply to config via apply_extras_params.py.

    Strategy id inferred from config.yml (first enabled EnhancedModelSignalStrategy), fallback to 'enhanced_ml_001'.
    Preference order for params source:
      1) evt.artifacts contains a JSON with {'top': [{'params': {...}}]} (nightly_hpo.py output)
      2) runs/ai_trader/<today>/hpo_suggestions/<SYM>.json aggregate top[0]
    Writes a temp best_params file and invokes apply_extras_params.py.
    """
    # Find best params payload
    artifacts = evt.get("artifacts") or []
    params_dict: Dict[str, Any] = {}
    try:
        for a in artifacts:
            p = Path(str(a))
            if not p.exists():
                continue

            # If artifact is a directory, search for JSON files inside
            json_files = []
            if p.is_dir():
                json_files = list(p.glob("*.json"))
            elif p.suffix == ".json":
                json_files = [p]

            for json_file in json_files:
                data = json.loads(json_file.read_text("utf-8"))
                # nightly_hpo.py --output file -> {'top': [{'params': {...}}]}
                if isinstance(data, dict) and isinstance(data.get("top"), list) and data["top"]:
                    cand = data["top"][0]
                    if isinstance(cand, dict) and isinstance(cand.get("params"), dict):
                        params_dict = cand["params"]
                        break
            if params_dict:
                break
    except Exception:
        params_dict = {}

    # Fallback: use hpo_suggestions directory
    if not params_dict:
        try:
            d = _runs_dir() / date.today().isoformat() / "hpo_suggestions"
            if d.exists():
                # Pick first file deterministically
                files = sorted(list(d.glob("*.json")))
                for fp in files:
                    data = json.loads(fp.read_text("utf-8"))
                    top = data.get("top") or []
                    if top and isinstance(top[0], dict) and isinstance(top[0].get("params"), dict):
                        params_dict = top[0]["params"]
                        break
        except Exception:
            params_dict = {}

    if not params_dict:
        raise RuntimeError("no_best_params_available")

    # Write temp best_params file
    out_dir = _runs_dir() / date.today().isoformat() / "hpo_apply"
    out_dir.mkdir(parents=True, exist_ok=True)
    best_fp = out_dir / "best_params.json"
    best_fp.write_text(json.dumps(params_dict, indent=2), encoding="utf-8")

    # Strategy id from config.yml
    strategy_id = "enhanced_ml_001"
    try:
        cfg = _cfg.get_raw() if hasattr(_cfg, "get_raw") else _cfg
        for s in (cfg.get("strategies") or []):
            if not s.get("enabled"):  # skip disabled
                continue
            mod = str(s.get("module") or "")
            cls = str(s.get("class") or "")
            if "enhanced_model_signal_strategy" in mod or "EnhancedModelSignalStrategy" in cls:
                sid = str(s.get("id") or "").strip()
                if sid:
                    strategy_id = sid
                    break
    except Exception:
        pass

    # Apply via existing tool (respects backups/invalidation)
    import sys as _sys, subprocess as _sp
    cmd = [_sys.executable, "apply_extras_params.py",
           "--hpo-params", str(best_fp),
           "--config-yaml", "config.yml",
           "--strategy-id", strategy_id,
           "--no-backup"]
    p = _sp.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"apply_extras_params failed rc={p.returncode}: {p.stderr[-300:]}")
    # Also persist params_dict as last known HPO best_params snapshot for Brain.
    try:
        _save_last_hpo_best_params(params_dict)
    except Exception:
        pass
    # Invalidate plan cache so next cycle uses new params
    try:
        inv = _runs_dir() / date.today().isoformat() / "cache" / "propose_plan.invalidate"
        inv.parent.mkdir(parents=True, exist_ok=True)
        inv.touch()
    except Exception:
        pass


async def _execute_fix_config(params: dict, job_id: str) -> dict:
    """Execute config fix action with safety checks.

    Supports two modes:
    1. config.yml updates (params: {path, value, merge})
    2. JSON policy updates (params: {path, key, value})

    Args:
        params: Action parameters containing:
            Mode 1 (config.yml):
                - path: Dot-separated config path (e.g., "commands.alpha_loop")
                - value: Value to set
                - merge: Whether to merge dicts (default: True)
            Mode 2 (JSON policies):
                - path: File path (e.g., "config/oof_policies.json")
                - key: Dot-separated key (e.g., "defaults.is_days")
                - value: Value to set
        job_id: Source job ID for audit

    Returns:
        Execution result
    """
    result = {
        "status": "error",
        "reason": ""
    }

    try:
        path = params.get("path")
        value = params.get("value")

        if not path or value is None:
            result["reason"] = "Missing required params: path, value"
            return result

        # Determine mode: JSON policy (has "key" param) or config.yml
        key = params.get("key")

        if key is not None:
            # Mode 2: JSON policy / alpha_config / HPO config update (file path + key)
            from scripts.ai_trader.config import json_policy_patcher, alpha_config_patcher, hpo_config_patcher

            # Route to appropriate patcher based on target file
            fname = Path(path).name
            if fname in {"alpha_config_enhanced.json", "alpha_config_enhanced_debug.json"}:
                # Alpha config (label_profile switching, regression profiles only)
                try:
                    cur = alpha_config_patcher.get(path, key)
                    if cur == value:
                        return {"status": "noop", "path": path, "key": key}
                except Exception:
                    pass
                patch_result = alpha_config_patcher.update(file_path=path, key=key, value=value)
            elif fname == "hpo_tsla_working.json":
                # HPO config (per-symbol TSLA HPO settings)
                try:
                    cur = hpo_config_patcher.get(path, key)
                    if cur == value:
                        return {"status": "noop", "path": path, "key": key}
                except Exception:
                    pass
                patch_result = hpo_config_patcher.update(file_path=path, key=key, value=value)
            else:
                # JSON policy update (oof_policies / gating_policies / ml_policy)
                try:
                    cur = json_policy_patcher.get(path, key)
                    if cur == value:
                        return {"status": "noop", "path": path, "key": key}
                except Exception:
                    pass
                patch_result = json_policy_patcher.update(file_path=path, key=key, value=value)

            if patch_result["ok"]:
                logger.info("json_policy_fix_applied",
                           file=path,
                           key=key,
                           backup=patch_result["backup"],
                           job_id=job_id)

                result = {
                    "status": "ok",
                    "path": path,
                    "key": key,
                    "backup": patch_result["backup"],
                    "old_value": str(patch_result["old_value"])[:100],
                    "new_value": str(patch_result["new_value"])[:100],
                    "merged": False
                }
            else:
                logger.error("json_policy_fix_failed",
                            file=path,
                            key=key,
                            error=patch_result["error"],
                            job_id=job_id)
                result = {
                    "status": "failed",
                    "reason": patch_result["error"]
                }

        else:
            # Mode 1: config.yml update (dot-path only)
            merge = params.get("merge", True)
            from scripts.ai_trader.config import config_patcher

            # No-op guard
            try:
                cur = config_patcher.get(path)
                if cur == value:
                    return {"status": "noop", "path": path}
            except Exception:
                pass

            patch_result = config_patcher.update(path, value, merge=merge)

            if patch_result["ok"]:
                logger.info("config_fix_applied",
                           path=path,
                           backup=patch_result["backup"],
                           job_id=job_id,
                           merged=patch_result["merged"])

                # Invalidate plan cache to trigger re-validation
                try:
                    invalidate_file = Path("cache/propose_plan.invalidate")
                    invalidate_file.parent.mkdir(parents=True, exist_ok=True)
                    invalidate_file.touch()
                except Exception:
                    pass

                result = {
                    "status": "ok",
                    "path": path,
                    "backup": patch_result["backup"],
                    "old_value": str(patch_result["old_value"])[:100],
                    "new_value": str(patch_result["new_value"])[:100],
                    "merged": patch_result["merged"]
                }
            else:
                logger.error("config_fix_failed",
                            path=path,
                            error=patch_result["error"],
                            job_id=job_id)
                result = {
                    "status": "failed",
                    "reason": patch_result["error"]
                }

    except Exception as e:
        result["status"] = "error"
        result["reason"] = str(e)
        logger.error("config_fix_execution_error", job_id=job_id, error=str(e), exc_info=True)

    return result


async def _execute_pause_ml(params: dict, job_id: str) -> dict:
    """Pause ML training by creating a lock file.

    Args:
        params: Action parameters containing:
            - reason: Reason for pausing (required)
        job_id: Source job ID for audit

    Returns:
        Execution result
    """
    result = {
        "status": "error",
        "reason": ""
    }

    try:
        reason = params.get("reason", "No predictive edge detected")

        dstr = date.today().isoformat()
        lock_file = _runs_dir() / dstr / "locks" / "ml_pause.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        lock_data = {
            "paused_at": datetime.now().isoformat(),
            "job_id": job_id,
            "reason": reason
        }

        lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")

        logger.info("ml_training_paused", job_id=job_id, reason=reason, lock_file=str(lock_file))

        result = {
            "status": "ok",
            "lock_file": str(lock_file),
            "reason": reason
        }

    except Exception as e:
        result["status"] = "error"
        result["reason"] = str(e)
        logger.error("pause_ml_execution_error", job_id=job_id, error=str(e), exc_info=True)

    return result


def _check_daily_quota(action_type: str) -> tuple[bool, str]:
    """Check if action is within daily quota.

    Daily quotas (per action type):
    - backfill: 5/day
    - fix_config: 10/day
    - queue_job: 3/day
    - pause_ml: 2/day
    - extend_window: 5/day

    Args:
        action_type: Type of action

    Returns:
        (is_allowed, reason) tuple
    """
    DAILY_QUOTAS = {
        "backfill": 9999,          # Effectively unlimited for Brain autonomy
        "fix_config": 9999,        # Effectively unlimited for Brain autonomy
        "queue_job": 9999,         # Effectively unlimited for Brain autonomy
        "pause_ml": 9999,          # Effectively unlimited for Brain autonomy
        "extend_window": 9999,     # Effectively unlimited for Brain autonomy
        "reduce_window": 9999,     # Effectively unlimited for Brain autonomy
        # Allow Brain to iterate on feature proposals freely; guardrails live in validators + feature_build
        "propose_features": 20000,
    }

    # No quota for actions not in the list
    if action_type not in DAILY_QUOTAS:
        return True, ""

    quota_limit = DAILY_QUOTAS[action_type]

    try:
        # Read today's actions from ml_actions.jsonl
        audit_file = _runs_dir() / date.today().isoformat() / "ml_actions.jsonl"
        if not audit_file.exists():
            return True, ""

        # Count successful executions of this action type today
        count = 0
        with audit_file.open("r") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("action") == action_type and entry.get("status") == "ok":
                        count += 1
                except json.JSONDecodeError:
                    continue

        if count >= quota_limit:
            return False, f"Daily quota exceeded: {count}/{quota_limit} for {action_type}"

        return True, ""

    except Exception as e:
        logger.error("quota_check_error", action=action_type, error=str(e))
        # On error, allow (fail open for availability)
        return True, ""


async def _execute_ml_actions(
    review_result: dict,
    job_id: str,
    prev_oof_metrics: Dict[str, Dict[str, float]] | None = None,
    cur_oof_metrics: Dict[str, Dict[str, float]] | None = None,
) -> None:
    """Execute next_actions from ML review with safety checks.

    Args:
        review_result: Brain review result containing 'next_actions'
        job_id: ML job ID for audit
    """
    actions = review_result.get("next_actions", [])
    if not actions:
        return

    logger.info("processing_ml_actions", job_id=job_id, action_count=len(actions))

    # Audit log file
    audit_file = _runs_dir() / date.today().isoformat() / "ml_actions.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)

    # Prioritize actions: data_quality_check → backfill → reduce_window → extend_window → queue_job → fix_config → pause/resume → wait/do_nothing/escalate
    try:
        prio = {
            "data_quality_check": 5,
            "backfill": 10,
            "reduce_window": 15,
            "extend_window": 20,
            "propose_features": 25,
            "queue_job": 30,
            "fix_config": 40,
            "pause_ml": 50,
            "resume_ml": 60,
            "wait": 90,
            "do_nothing": 95,
            "escalate": 98,
        }
        actions = sorted(actions, key=lambda a: prio.get(str(a.get("action") or ""), 999))
        # Enforce single action per review cycle to avoid overloading platform
        if len(actions) > 1:
            actions = actions[:1]
    except Exception:
        pass

    state = _load_opt_state()
    # Load recent action memory for confidence calibration (last 7 days)
    try:
        from scripts.ai_trader.brain.policy.memory import (
            summarize_actions as _summarize_actions,
            summarize_actions_by_symbol as _summarize_by_symbol,
            summarize_actions_clickhouse as _summarize_ch,
        )
        # Try ClickHouse (if enabled), else fallback to JSONL summarizers
        try:
            pol_ml = ((_cfg.get('policies') or {}).get('ml') or {})
            mem_cfg = (pol_ml.get('memory_source') or {})
        except Exception:
            mem_cfg = {}
        use_ch = bool(mem_cfg.get('use_clickhouse', False))
        if use_ch:
            table = str(mem_cfg.get('table') or 'llm.ai_ml_actions')
            ma, mas = _summarize_ch(
                table,
                lookback_days=int(mem_cfg.get('lookback_days', 7) or 7),
                max_rows=int(mem_cfg.get('max_rows', 20000) or 20000),
                action_col=str(mem_cfg.get('action_col', 'action')),
                status_col=str(mem_cfg.get('status_col', 'status')),
                ts_col=str(mem_cfg.get('ts_col', 'ts')),
                params_col=str(mem_cfg.get('params_col', 'params')),
            )
            memory_summary = ma or {}
            memory_by_symbol = mas or {}
            if not memory_summary:
                # Fallback if CH query returned empty
                memory_summary = _summarize_actions(str(_runs_dir()), days=7)
                memory_by_symbol = _summarize_by_symbol(str(_runs_dir()), days=7)
        else:
            memory_summary = _summarize_actions(str(_runs_dir()), days=7)
            memory_by_symbol = _summarize_by_symbol(str(_runs_dir()), days=7)
    except Exception:
        memory_summary = {}
        memory_by_symbol = {}
    # Summarize before/after OOF metrics (mean IC/AUC/coverage across symbols) for reward logging
    def _summarize_oof(oof: Dict[str, Dict[str, float]] | None) -> Optional[Dict[str, float]]:
        if not oof:
            return None
        ic_vals: List[float] = []
        auc_vals: List[float] = []
        cov_vals: List[float] = []
        for vals in oof.values():
            if not isinstance(vals, dict):
                continue
            ic = vals.get("ic")
            auc = vals.get("auc")
            cov = vals.get("coverage")
            if isinstance(ic, (int, float)) and not (abs(ic) != ic or ic != ic):
                ic_vals.append(float(ic))
            if isinstance(auc, (int, float)):
                auc_vals.append(float(auc))
            if isinstance(cov, (int, float)):
                cov_vals.append(float(cov))
        if not ic_vals and not auc_vals and not cov_vals:
            return None
        summary: Dict[str, float] = {}
        if ic_vals:
            summary["ic"] = sum(ic_vals) / len(ic_vals)
        if auc_vals:
            summary["auc"] = sum(auc_vals) / len(auc_vals)
        if cov_vals:
            summary["coverage"] = sum(cov_vals) / len(cov_vals)
        return summary

    before_summary = _summarize_oof(prev_oof_metrics) if prev_oof_metrics else None
    after_summary = _summarize_oof(cur_oof_metrics) if cur_oof_metrics else None
    delta_summary: Optional[Dict[str, float]] = None
    if before_summary and after_summary:
        delta_summary = {}
        if "ic" in before_summary and "ic" in after_summary:
            delta_summary["delta_ic"] = after_summary["ic"] - before_summary["ic"]
        if "auc" in before_summary and "auc" in after_summary:
            delta_summary["delta_auc"] = after_summary["auc"] - before_summary["auc"]
        if "coverage" in before_summary and "coverage" in after_summary:
            delta_summary["delta_coverage"] = after_summary["coverage"] - before_summary["coverage"]

    # Try to infer stage and branch from review_result / action metadata
    stage_default = (review_result.get("stage") or "").lower() if isinstance(review_result, dict) else ""
    # Map action types to high-level stages for reward attribution
    stage_map = {
        "propose_features": "alpha",
        "feature_build": "alpha",
        "feature_eval": "alpha",
        "feature_apply": "alpha",
        "feature_recompute": "alpha",
        "backfill": "data",
        "bulk_backfill": "data",
        "data_quality_check": "data",
        "queue_job": "model",
        "extend_window": "model",
        "reduce_window": "model",
        "ensemble_sweep": "model",
        "hpo_cli": "model",
        "alpha_loop": "model",
        "fix_config": "execution",
        "pause_ml": "execution",
        "resume_ml": "execution",
        "wait": "execution",
        "do_nothing": "execution",
        "escalate": "execution",
    }

    for act in actions:
        action_type = act.get("action")
        params = act.get("params", {})
        confidence = float(act.get("confidence", 0.0))
        branch_id = act.get("branch_id") or review_result.get("branch_id") if isinstance(review_result, dict) else None
        stage = act.get("stage") or stage_default or stage_map.get(str(action_type), "")

        audit_entry = {
            "ts": datetime.now().isoformat(),
            "job_id": job_id,
            "action": action_type,
            "params": params,
            "confidence": confidence,
            "stage": stage or None,
            "branch_id": branch_id or None,
            "before_metrics": before_summary,
            "after_metrics": after_summary,
            "delta": delta_summary,
            "status": "skipped",
            "reason": ""
        }
        # mark attempt start for external readers
        audit_entry["started_at"] = audit_entry["ts"]

        try:
            # Confidence threshold check (configurable)
            try:
                pol_ml = ((_cfg.get('policies') or {}).get('ml') or {})
                # Per-action or default floor
                conf_floor_default = float(pol_ml.get('actions_conf_floor', 0.6))
                min_conf_map = (pol_ml.get('min_conf_by_action') or {})
                conf_floor = float(min_conf_map.get(str(action_type), conf_floor_default))
                # Suggest a sensible default for data_quality_check if not specified
                if str(action_type) == 'data_quality_check' and 'data_quality_check' not in min_conf_map:
                    conf_floor = min(conf_floor, 0.4)

                cal_cfg = (pol_ml.get('confidence_calibration') or {})
                base = float(cal_cfg.get('base', 0.6))
                hist_w = float(cal_cfg.get('history_weight', 0.4))
                skip_streak = int(cal_cfg.get('skip_fail_streak', 2))
            except Exception:
                conf_floor = 0.6
                base, hist_w, skip_streak = 0.6, 0.4, 2

            # In training-only mode (ML Brain loop), be more exploratory:
            # - lower confidence floor
            # - allow more retries before fail-streak lock kicks in
            if TRAINING_ONLY:
                conf_floor = min(conf_floor, 0.4)
                skip_streak = max(skip_streak, 5)

            # Apply memory-based calibration: scale raw confidence by 0.6 + 0.4*success_rate
            try:
                ms = memory_summary.get(str(action_type)) or {}
                sr_action = float(ms.get("success_rate", 0.5))
                # Derive symbol list from params
                sym_list = []
                if isinstance(params, dict):
                    if isinstance(params.get("symbols"), list):
                        sym_list = [str(s).upper() for s in params.get("symbols") if s]
                    elif isinstance(params.get("symbols"), str):
                        sym_list = [s.strip().upper() for s in params.get("symbols").split(",") if s.strip()]
                    elif isinstance(params.get("symbol"), str):
                        sym_list = [str(params.get("symbol")).upper()]
                # Combine per-symbol success rates if available
                sr_syms = []
                for s in sym_list:
                    sr_map = ((memory_by_symbol.get(str(action_type)) or {}).get(s) or {})
                    if sr_map:
                        try:
                            sr_syms.append(float(sr_map.get("success_rate", 0.5)))
                        except Exception:
                            pass
                sr = (sum(sr_syms) / len(sr_syms)) if sr_syms else sr_action
                # Compute final confidence multiplier and clamp
                mult = max(0.0, min(1.0, base + hist_w * sr))
                confidence_final = float(confidence) * mult

                # NEW: Apply model-specific gating for ML training/evaluation actions
                # Check if this is a model-related action that should use ClickHouse model stats
                model_actions = {"hpo_cli", "alpha_loop", "queue_job", "oof", "ensemble_sweep"}
                if str(action_type) in model_actions and sym_list:
                    # Extract model parameters from action params
                    model_family = ""
                    horizon = ""
                    if isinstance(params, dict):
                        model_family = params.get("model_family", params.get("model", "xgb"))
                        horizon = params.get("horizon", params.get("label", "h5"))

                    # Apply model gating for each symbol
                    model_multipliers = []
                    for s in sym_list:
                        try:
                            model_mult = _get_model_confidence_multiplier(
                                action=act,
                                symbol=s,
                                model_family=str(model_family),
                                horizon=str(horizon)
                            )
                            model_multipliers.append(model_mult)

                            # Hard gate: if any symbol's model is disabled (mult=0), skip entire action
                            if model_mult == 0.0:
                                audit_entry["reason"] = f"model_disabled_for_{s}"
                                audit_entry["model_gating"] = {
                                    "symbol": s,
                                    "model_family": model_family,
                                    "horizon": horizon,
                                    "multiplier": 0.0,
                                    "gated": True
                                }
                                logger.warning(
                                    "ml_action_gated_by_model_stats",
                                    action=action_type,
                                    symbol=s,
                                    model_family=model_family,
                                    horizon=horizon,
                                    reason="model_disabled",
                                    job_id=job_id
                                )
                                # Set confidence to 0 to trigger skip later
                                confidence_final = 0.0
                                break
                        except Exception as e:
                            logger.error(
                                "model_gating_error",
                                action=action_type,
                                symbol=s,
                                error=str(e),
                                exc_info=True
                            )
                            # On error, don't gate (fail open)

                    # If not gated, apply average multiplier
                    if model_multipliers and confidence_final > 0.0:
                        avg_model_mult = sum(model_multipliers) / len(model_multipliers)
                        original_conf = confidence_final
                        confidence_final *= avg_model_mult
                        audit_entry["model_gating"] = {
                            "symbols": sym_list,
                            "model_family": model_family,
                            "horizon": horizon,
                            "multiplier": round(avg_model_mult, 3),
                            "original_confidence": round(original_conf, 4),
                            "adjusted_confidence": round(confidence_final, 4),
                            "gated": False
                        }
                        logger.info(
                            "ml_action_confidence_adjusted_by_model",
                            action=action_type,
                            symbols=sym_list,
                            model_multiplier=round(avg_model_mult, 3),
                            original_confidence=round(original_conf, 4),
                            adjusted_confidence=round(confidence_final, 4),
                            job_id=job_id
                        )

                # Compute fail streak (max over symbols, else action-level)
                streak_candidates = []
                if sym_list and (memory_by_symbol.get(str(action_type))):
                    for s in sym_list:
                        try:
                            streak_candidates.append(int(memory_by_symbol[str(action_type)][s].get("recent_fail_streak", 0)))
                        except Exception:
                            pass
                if streak_candidates:
                    recent_fail_streak = max(streak_candidates)
                else:
                    recent_fail_streak = int(ms.get("recent_fail_streak", 0))
            except Exception:
                confidence_final = confidence
                recent_fail_streak = 0
            audit_entry["confidence_final"] = round(confidence_final, 4)

            # Anti-repeat: skip if recent fail streak >= configured threshold for the same action type/symbols
            if (not TRAINING_ONLY) and recent_fail_streak >= skip_streak:
                audit_entry["reason"] = f"recent_fail_streak>={recent_fail_streak}"
                logger.info("ml_action_skipped_recent_fail_streak", action=action_type, streak=recent_fail_streak, job_id=job_id)
                continue

            if confidence_final < conf_floor:
                audit_entry["reason"] = f"Low confidence_final: {confidence_final:.2f} < {conf_floor} (raw={confidence:.2f})"
                logger.info("ml_action_skipped_low_confidence", action=action_type, confidence=confidence, threshold=conf_floor, job_id=job_id)
                continue

            # Skip pause if disabled globally
            try:
                allow_pause = bool(((_cfg.get("policies") or {}).get("ml") or {}).get("allow_pause", False))
            except Exception:
                allow_pause = False
            if action_type == "pause_ml" and not allow_pause:
                audit_entry["reason"] = "pause_disabled"
                logger.info("ml_action_skipped_pause_disabled", job_id=job_id)
                continue

            # Whitelist check (expanded)
            if action_type not in {"backfill", "extend_window", "propose_features", "queue_job", "fix_config", "pause_ml", "resume_ml", "reduce_window", "data_quality_check", "feature_recompute", "wait", "do_nothing", "escalate"}:
                audit_entry["reason"] = f"Action '{action_type}' not in whitelist"
                logger.warning("ml_action_not_whitelisted", action=action_type, job_id=job_id)
                continue

            # TTL lock check (15 min deduplication)
            if (not TRAINING_ONLY) and _ttl_lock(f"ml_action_{action_type}", ttl_sec=900):
                audit_entry["reason"] = "Action locked by TTL (900s)"
                logger.info("ml_action_locked", action=action_type, job_id=job_id)
                continue

            # Fingerprint TTL (1h) to avoid repeating identical actions
            try:
                import hashlib, json as _J
                fp_raw = _J.dumps({"a": action_type, "p": params}, sort_keys=True)
                fp = hashlib.sha1(fp_raw.encode("utf-8")).hexdigest()[:16]
                if _ttl_lock(f"ml_action_fp_{fp}", ttl_sec=3600):
                    audit_entry["reason"] = "Action fingerprint locked (3600s)"
                    logger.info("ml_action_fp_locked", action=action_type, job_id=job_id)
                    continue
            except Exception:
                pass

            # Daily quota check
            quota_allowed, quota_reason = _check_daily_quota(action_type)
            if not quota_allowed:
                audit_entry["reason"] = quota_reason
                logger.warning("ml_action_quota_exceeded", action=action_type, reason=quota_reason, job_id=job_id)
                continue

            # Execute action
            if action_type == "backfill":
                exec_result = await _execute_backfill(params, job_id)
                audit_entry["status"] = exec_result["status"]
                audit_entry["command"] = exec_result.get("command", "")
                audit_entry["stdout_tail"] = exec_result.get("stdout", "")[-500:]
                audit_entry["stderr_tail"] = exec_result.get("stderr", "")[-500:]
                audit_entry["returncode"] = exec_result.get("returncode")
                _bump_attempt(state, "data")

            elif action_type == "extend_window":
                exec_result = await _record_extend_window_hint(params, job_id)
                audit_entry["status"] = exec_result["status"]
                audit_entry["hint_recorded"] = exec_result.get("hint_recorded", False)
                audit_entry["auto_triggered"] = exec_result.get("auto_triggered", False)
                if exec_result.get("queued_job_id"):
                    audit_entry["queued_job_id"] = exec_result["queued_job_id"]
                audit_entry["reason"] = exec_result.get("reason", "")
                _bump_attempt(state, "data")

            elif action_type == "reduce_window":
                exec_result = await _record_reduce_window_hint(params, job_id)
                audit_entry["status"] = exec_result["status"]
                audit_entry["hint_recorded"] = exec_result.get("hint_recorded", False)
                audit_entry["auto_triggered"] = exec_result.get("auto_triggered", False)
                if exec_result.get("queued_job_id"):
                    audit_entry["queued_job_id"] = exec_result["queued_job_id"]
                audit_entry["reason"] = exec_result.get("reason", "")
                _bump_attempt(state, "data")

            elif action_type == "data_quality_check":
                exec_result = await _execute_data_quality_check(params, job_id)
                audit_entry["status"] = exec_result.get("status", "unknown")
                audit_entry["result"] = {k: v for k, v in exec_result.items() if k not in {"stdout", "stderr"}}
                # Auto-policy: if coverage now OK → enqueue HPO; if still low → feature recompute for low symbols
                try:
                    report_path = exec_result.get("report")
                    if report_path:
                        rep = json.loads(Path(report_path).read_text("utf-8"))
                        ar = rep.get("active_report") or {}
                        if isinstance(ar, dict):
                            min_cov = float((ar.get("min_coverage") if ar.get("min_coverage") is not None else params.get("min_coverage", 0.98)) or 0.98)
                            start = ar.get("start"); end = ar.get("end")
                            syms = []
                            low = []
                            for s in (ar.get("symbols") or []):
                                if isinstance(s, dict):
                                    syms.append(str(s.get("symbol")))
                                    cov = float(s.get("coverage", 0.0) or 0.0)
                                    if cov < min_cov:
                                        low.append(str(s.get("symbol")))
                            # Auto follow-up
                            if low:
                                # Run feature recompute for low coverage symbols
                                fr_params = {"symbols": low, "start": start, "end": end}
                                fr_res = await _execute_feature_recompute(fr_params, job_id)
                                # Write a separate audit entry
                                auto_entry = {
                                    "ts": datetime.now().isoformat(),
                                    "job_id": job_id,
                                    "action": "feature_recompute",
                                    "params": fr_params,
                                    "status": fr_res.get("status", "unknown"),
                                    "result": {k: v for k, v in fr_res.items() if k not in {"stdout", "stderr"}}
                                }
                                try:
                                    with audit_file.open("a", encoding="utf-8") as f:
                                        f.write(json.dumps(auto_entry, ensure_ascii=False) + "\n")
                                except Exception:
                                    pass
                            elif syms:
                                # Coverage OK → enqueue HPO automatically for these symbols (lightweight defaults)
                                hpo_params = {"job_type": "alpha_loop", "params": {"symbols": syms, "n_trials": 30, "timeout_hours": 1.0}}
                                qres = await _enqueue_ml_job(hpo_params, job_id)
                                auto_entry = {
                                    "ts": datetime.now().isoformat(),
                                    "job_id": job_id,
                                    "action": "queue_job",
                                    "params": hpo_params,
                                    "status": qres.get("status", "unknown"),
                                    "queued_job_id": qres.get("queued_job_id")
                                }
                                try:
                                    with audit_file.open("a", encoding="utf-8") as f:
                                        f.write(json.dumps(auto_entry, ensure_ascii=False) + "\n")
                                except Exception:
                                    pass
                except Exception as e:
                    logger.warning("dq_autopolicy_failed", error=str(e))
            elif action_type == "feature_recompute":
                exec_result = await _execute_feature_recompute(params, job_id)
                audit_entry["status"] = exec_result.get("status", "unknown")
                audit_entry["result"] = {k: v for k, v in exec_result.items() if k not in {"stdout", "stderr"}}

            elif action_type == "queue_job":
                # Allow Brain to queue jobs autonomously without cooldown restrictions
                # The Brain itself will decide when to stop based on performance metrics
                exec_result = await _enqueue_ml_job(params, job_id)
                audit_entry["status"] = exec_result["status"]
                audit_entry["queued_job_id"] = exec_result.get("queued_job_id")
                audit_entry["reason"] = exec_result.get("reason", "")
                audit_entry["status"] = exec_result["status"]
                _bump_attempt(state, "hpo")

            elif action_type == "fix_config":
                # Profit-first guard: disallow lowering confidence thresholds (conf_floor) via fix_config
                try:
                    k = str(params.get("key") or "")
                    p = str(params.get("path") or "")
                    v_new = params.get("value")
                    if v_new is not None and k:
                        lowered = False
                        if "conf_floor" in k:
                            # Read current value; if new < current → block
                            try:
                                from scripts.ai_trader.config import json_policy_patcher as _jpp
                                cur = _jpp.get(p, k)
                                if cur is not None:
                                    try:
                                        cur_f = float(cur)
                                        new_f = float(v_new)
                                        if new_f < cur_f:
                                            lowered = True
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        if lowered:
                            audit_entry["status"] = "skipped"
                            audit_entry["reason"] = "profit_first_guard_conf_floor_no_lowering"
                            logger.info("fix_config_blocked_by_profit_guard", key=k, old=cur, new=v_new, job_id=job_id)
                            # Write audit and continue to next action without executing
                            try:
                                with audit_file.open("a", encoding="utf-8") as f:
                                    f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")
                            except Exception:
                                pass
                            continue
                except Exception:
                    pass
                exec_result = await _execute_fix_config(params, job_id)
                audit_entry["status"] = exec_result["status"]
                audit_entry["path"] = exec_result.get("path", "")
                audit_entry["key"] = exec_result.get("key", "")  # For JSON policy mode
                audit_entry["backup"] = exec_result.get("backup", "")
                audit_entry["old_value"] = exec_result.get("old_value", "")
                audit_entry["new_value"] = exec_result.get("new_value", "")
                audit_entry["merged"] = exec_result.get("merged", False)
                audit_entry["reason"] = exec_result.get("reason", "")
                _bump_attempt(state, "params")

            elif action_type == "propose_features":
                # Write candidate specs to catalog and enqueue feature_build
                try:
                    # BEFORE updating the catalog, run a blocking self-healing
                    # pass for any cross-asset symbol dependencies inferred
                    # from the proposed specs. This ensures that by the time
                    # _write_features_catalog() performs its dry-runs, the
                    # underlying data is as complete as possible, avoiding
                    # transient all-NULL features caused purely by missing
                    # history for symbols like SPY/QQQ.
                    try:
                        specs = params.get("specs") or []
                        dep_symbols = _infer_symbol_dependencies_from_specs(
                            specs if isinstance(specs, list) else []
                        )
                        if dep_symbols:
                            bf_params = {
                                "symbols": dep_symbols,
                                "days": int(params.get("backfill_days", 60) or 60),
                                "bar_size": "1m",
                            }
                            bf_res = await _execute_backfill(bf_params, job_id)
                            logger.info(
                                "auto_backfill_for_feature_deps",
                                job_id=job_id,
                                symbols=dep_symbols,
                                status=bf_res.get("status"),
                            )
                    except Exception as exc:
                        logger.warning(
                            "auto_backfill_for_feature_deps_failed",
                            job_id=job_id,
                            error=str(exc),
                        )

                    ok, reason, required_sources = _write_features_catalog(params)
                    audit_entry["status"] = "ok" if ok else "failed"
                    audit_entry["reason"] = reason
                    if ok:
                        try:
                            _ensure_resample_sources(required_sources)
                        except Exception as exc:
                            logger.warning("resample_source_ensure_failed", error=str(exc), job_id=job_id)
                        exec_result = await _enqueue_ml_job({"job_type": "feature_build", "params": {}}, job_id)
                        audit_entry["queued_job_id"] = exec_result.get("queued_job_id")
                except Exception as e:
                    audit_entry["status"] = "error"
                    audit_entry["reason"] = str(e)

            elif action_type == "pause_ml":
                if _eligible_to_pause(state):
                    exec_result = await _execute_pause_ml(params, job_id)
                    audit_entry["status"] = exec_result["status"]
                    audit_entry["lock_file"] = exec_result.get("lock_file", "")
                    audit_entry["reason"] = exec_result.get("reason", "")
                else:
                    audit_entry["status"] = "deferred"
                    audit_entry["reason"] = "pause_not_eligible_yet"

            elif action_type == "resume_ml":
                # Remove pause lock if present
                lockp = _runs_dir() / date.today().isoformat() / "locks" / "ml_pause.lock"
                try:
                    if lockp.exists():
                        lockp.unlink()
                    audit_entry["status"] = "ok"
                except Exception as e:
                    audit_entry["status"] = "failed"
                    audit_entry["reason"] = str(e)

            elif action_type in {"wait", "do_nothing"}:
                audit_entry["status"] = "ok"
                audit_entry["reason"] = "noop"

            elif action_type == "escalate":
                # Write escalation entry only
                try:
                    esc = _runs_dir() / date.today().isoformat() / "escalations.jsonl"
                    esc.parent.mkdir(parents=True, exist_ok=True)
                    with esc.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "ts": datetime.now().isoformat(),
                            "job_id": job_id,
                            "reason": params.get("reason") if isinstance(params, dict) else None
                        }, ensure_ascii=False) + "\n")
                    audit_entry["status"] = "ok"
                except Exception as e:
                    audit_entry["status"] = "failed"
                    audit_entry["reason"] = str(e)

        except Exception as e:
            audit_entry["status"] = "error"
            audit_entry["error"] = str(e)
            logger.error("ml_action_execution_failed", action=action_type, job_id=job_id, error=str(e), exc_info=True)

        # Always write audit entry; set executed_at for non-skipped attempts
        if audit_entry.get("status") != "skipped" and "executed_at" not in audit_entry:
            audit_entry["executed_at"] = datetime.now().isoformat()
        # Always write audit entry
        try:
            with audit_file.open("a") as f:
                f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("audit_write_failed", error=str(e))

        # Stop after first successful action to avoid stacking
        if audit_entry.get("status") == "ok":
            break

    # Persist updated optimization state
    _save_opt_state(state)


def main() -> int:
    if not _acquire_singleton_lock():
        # Another instance likely running
        return 0
    try:
        asyncio.run(consume())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        logger.warning("control_consumer_exit", err=str(e), exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
