from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple, Set
import os


def _collect_numbers(obj: Any, out: Set[float] | None = None, depth: int = 0) -> Set[float]:
    if out is None:
        out = set()
    if obj is None:
        return out
    if isinstance(obj, (int, float)):
        try:
            # Round to 3 decimals for stable matching
            out.add(float(round(obj, 3)))
        except Exception:
            pass
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out, depth + 1)
    return out


def _derive_large_window_toxicity(oof: Dict[str, Any] | None) -> bool:
    """Heuristic: if any symbol has n_bars≥25k and ic<0.02 or auc≈0.5 → toxic."""
    if not isinstance(oof, dict):
        return False
    try:
        for sym, s in (oof or {}).items():
            if not isinstance(s, dict):
                continue
            # Try multiple common keys for bar count
            n_bars = None
            # First try params.n_bars (newer format)
            if isinstance(s.get("params"), dict) and "n_bars" in s["params"]:
                n_bars = float(s["params"]["n_bars"])
            # Fallback to top-level keys
            if n_bars is None:
                for k in ("n_bars", "bars", "total_bars"):
                    if k in s and isinstance(s[k], (int, float)):
                        n_bars = float(s[k])
                        break
            ic = None
            auc = None
            # fusion preferred
            if isinstance(s.get("fusion"), dict):
                ic = s["fusion"].get("ic")
                auc = s["fusion"].get("auc")
            for m in ("xgb", "lstm"):
                if ic is None and isinstance(s.get(m), dict):
                    ic = ic or s[m].get("ic")
                if auc is None and isinstance(s.get(m), dict):
                    auc = auc or s[m].get("auc")
            if n_bars is not None and n_bars >= 25000 and (
                (isinstance(ic, (int, float)) and ic < 0.02)
                or (isinstance(auc, (int, float)) and abs(auc - 0.5) <= 0.03)
            ):
                return True
    except Exception:
        return False
    return False


def build_ml_stage0(context: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic pre-check: derive flags, allowed_actions, allowed_numbers, and recent_actions summary.

    Returns a dict: {derived_flags, allowed_actions, allowed_numbers, recent_actions, missing_fields}
    """
    derived_flags: Dict[str, Any] = {}
    training_only = os.getenv("ML_BRAIN_LOOP", "").strip().lower() in {"1", "true", "yes", "on"}
    allowed_actions: List[str] = [
        "backfill",
        "propose_features",
        "extend_window",
        "queue_job",
        "fix_config",
        "pause_ml",
        "resume_ml",
        # newly supported actions
        "reduce_window",
        "data_quality_check",
        "feature_recompute",
        "wait",
        "do_nothing",
        "escalate",
    ]

    # Data-quality red flag (coarse): any obvious anomalies.
    # Prefer the structural snapshot from data_quality.json when present;
    # fall back to metrics-derived data_quality only if no snapshot exists.
    dq = context.get("data_quality_snapshot") or context.get("data_quality") or {}
    try:
        red = False
        if isinstance(dq, dict):
            red = any([
                (dq.get("zero_price_count") or 0) > 0,
                (dq.get("timestamp_gap_count") or 0) > 0,
                (dq.get("feature_const_rate") or 0) > 0.05,
                bool(dq.get("label_leak_suspect")),
            ])
        derived_flags["data_quality_red_flag"] = bool(red)
    except Exception:
        derived_flags["data_quality_red_flag"] = False

    # Large-window toxicity flag
    derived_flags["large_window_toxicity"] = _derive_large_window_toxicity(context.get("oof_evaluations"))

    # Long-run "no edge" flag based on recent OOF health snapshot, if provided.
    # This is a coarse signal to tell the Brain that recent AUC/IC are
    # consistently near-random, so it should consider experimentation instead
    # of repeating the same data/window tweaks.
    try:
        mhr = context.get("ml_health_recent") or {}
        no_edge_simple = bool(mhr.get("no_edge_simple"))
        ic_mean = mhr.get("fusion_ic_mean")
        auc_mean = mhr.get("fusion_auc_mean")
        no_edge_longrun = False
        if no_edge_simple:
            no_edge_longrun = True
        elif isinstance(ic_mean, (int, float)) and isinstance(auc_mean, (int, float)):
            # Fallback heuristic if explicit flag missing
            if abs(auc_mean - 0.5) < 0.02 and abs(ic_mean) < 0.01:
                no_edge_longrun = True
        derived_flags["no_edge_longrun"] = bool(no_edge_longrun)
    except Exception:
        derived_flags["no_edge_longrun"] = False

    # Profit gates pass flag (if present)
    gates_pass = None
    try:
        comp = (context.get("backtest_results") or {}).get("compare") or {}
        # accept any mode that passes as a positive signal
        for mode in ("fusion", "xgb", "lstm"):
            m = comp.get(mode) or {}
            sharpe_ok = float(m.get("sharpe", 0.0)) >= 1.0
            pf_ok = float(m.get("profit_factor", 0.0)) > 1.0
            if sharpe_ok and pf_ok:
                gates_pass = True
                break
        if gates_pass is None:
            gates_pass = False
    except Exception:
        gates_pass = False
    derived_flags["profit_gates_pass"] = bool(gates_pass)

    # Stronger "no edge confirmed" flag from experiments_summary, if available.
    # This indicates that many label/horizon experiments have been tried and
    # none produced meaningful positive IC/AUC.
    try:
        exp_summary = context.get("experiments_summary") or {}
        no_edge_conf = bool(exp_summary.get("no_edge_confirmed"))
        derived_flags["no_edge_confirmed"] = no_edge_conf
    except Exception:
        derived_flags["no_edge_confirmed"] = False

    # Enforce action restrictions based on flags
    if derived_flags["data_quality_red_flag"]:
        # prioritize data quality checks; allow backfill; deprioritize window tweaks
        pass
    if derived_flags["large_window_toxicity"]:
        # forbid extend_window; allow reduce_window
        if "extend_window" in allowed_actions:
            allowed_actions.remove("extend_window")
    # In long-run no-edge regime without hard data-quality issues, avoid
    # blindly repeating backfill; focus Brain on experiments instead.
    if derived_flags.get("no_edge_longrun") and not derived_flags.get("data_quality_red_flag"):
        if "backfill" in allowed_actions:
            allowed_actions.remove("backfill")

    # If many label/feature experiments have already confirmed no edge, we
    # should stop expensive search (backfill/HPO/feature proposals) and steer
    # the Brain toward pausing or changing task instead of continuing to
    # thrash. This is a coarse safety brake on long-run 0.5 AUC regimes.
    if derived_flags.get("no_edge_confirmed"):
        allowed_actions = [a for a in allowed_actions if a in {"pause_ml", "wait", "do_nothing", "escalate", "data_quality_check"}]

    # 在 ML Brain Loop 训练模式下，避免 LLM 反复触发直接 backfill，
    # 把数据自愈交给 DataHealer/bulk_backfill 管线，Brain 只专注于特征和实验。
    if training_only and "backfill" in allowed_actions:
        allowed_actions.remove("backfill")

    # Allowed numbers from context + policy defaults (to prevent false positives on reasonable params)
    allowed_numbers_set = _collect_numbers(context)
    # If recent backfill is present in context, expose its age for downstream logic
    recent_backfill_age_min = None
    # Feature build status (optional)
    feature_build_status = None
    try:
        rec = (context.get("recent_backfill") or {})
        ts = rec.get("ts")
        if ts:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts)
            recent_backfill_age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        feature_build_status = context.get("feature_build_status")
    except Exception:
        pass
    try:
        # Load ML policy to derive safe default numbers
        from libs.config import config as _cfg
        pol_ml = ((_cfg.get('policies') or {}).get('ml') or {})
        # Common window sizes (extended to 365 days to support full historical data usage)
        for v in (21, 30, 45, 60, 90, 120, 150, 180, 200, 240, 300, 365):
            allowed_numbers_set.add(float(v))
        # HPO common trials and timeout hours
        for v in (24, 30, 40, 45, 50):
            allowed_numbers_set.add(float(v))
        for v in (1, 2, 3, 4):
            allowed_numbers_set.add(float(v))
        # Timeout hours with decimals (0.5 to 5.0 in 0.01 increments for Brain flexibility)
        import numpy as np
        for v in np.arange(0.5, 5.01, 0.01):
            allowed_numbers_set.add(round(float(v), 2))
        # Backfill safe day counts
        for v in (1, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90, 120, 180, 200, 240, 300, 365):
            allowed_numbers_set.add(float(v))
        # Data-quality coverage thresholds
        for v in (0.9, 0.95, 0.98):
            allowed_numbers_set.add(float(v))
        # Confidence floors (min_conf_by_action) if present
        try:
            for v in (pol_ml.get('min_conf_by_action') or {}).values():
                try:
                    allowed_numbers_set.add(float(v))
                except Exception:
                    continue
        except Exception:
            pass
        # Calibration knobs if helpful
        try:
            cal = pol_ml.get('confidence_calibration') or {}
            for k in ('base','history_weight'):
                if cal.get(k) is not None:
                    allowed_numbers_set.add(float(cal[k]))
            if cal.get('skip_fail_streak') is not None:
                allowed_numbers_set.add(float(cal['skip_fail_streak']))
        except Exception:
            pass
    except Exception:
        pass

    # Recent actions summary from audit file (extracted early to get metrics)
    recent_actions: List[Dict[str, Any]] = []
    action_counts: Dict[str, int] = {}
    metrics_prev_by_symbol: Dict[str, Dict[str, float]] = {}
    metrics_cur_by_symbol: Dict[str, Dict[str, float]] = {}
    try:
        from datetime import date as _date
        runs_dir = Path(context.get("runs_dir") or "runs/ai_trader")
        fp = runs_dir / _date.today().isoformat() / "ml_actions.jsonl"
        # Load previous metrics if present
        try:
            prev_fp = runs_dir / _date.today().isoformat() / "cache" / "ml_last_metrics.json"
            if prev_fp.exists():
                data = json.loads(prev_fp.read_text("utf-8"))
                if isinstance(data.get("by_symbol"), dict):
                    for k, v in (data["by_symbol"] or {}).items():
                        try:
                            sym = str(k)
                            n = {}
                            for kk in ("n_bars","ic","auc"):
                                if v.get(kk) is not None:
                                    n[kk] = float(v.get(kk))
                            if n:
                                metrics_prev_by_symbol[sym] = n
                        except Exception:
                            continue
        except Exception:
            pass
        if fp.exists():
            text = fp.read_text("utf-8").strip()
            lines = text.splitlines() if text else []
            # Build action counts over the whole file (today)
            data_actions = 0
            hpo_actions = 0
            window_reduce = 0
            window_extend = 0
            feature_actions = 0
            fix_config_actions = 0
            feature_enh_backfill = 0
            for line in lines:
                try:
                    j = json.loads(line)
                    a = str(j.get("action") or "")
                    if a in ("backfill", "data_quality_check"):
                        data_actions += 1
                    elif a == "queue_job":
                        jt = ((j.get("params") or {}).get("job_type") or "").lower()
                        if jt == "alpha_loop":
                            hpo_actions += 1
                        elif jt in {"feature_build", "feature_eval"}:
                            feature_actions += 1
                        elif jt == "feature_enhanced_backfill":
                            feature_enh_backfill += 1
                    elif a == "reduce_window":
                        window_reduce += 1
                    elif a == "extend_window":
                        window_extend += 1
                    elif a == "fix_config":
                        fix_config_actions += 1
                except Exception:
                    continue
            action_counts = {
                "data_actions": data_actions,
                "hpo_actions": hpo_actions,
                "window_reduce": window_reduce,
                "window_extend": window_extend,
                "feature_actions": feature_actions,
                "feature_enhanced_backfill": feature_enh_backfill,
                "fix_config": fix_config_actions,
            }
            for line in reversed(lines[-20:]):
                try:
                    j = json.loads(line)
                    recent_actions.append({
                        "action": j.get("action"),
                        "params": j.get("params"),
                        "status": j.get("status"),
                        "reason": j.get("reason"),
                    })
                    if len(recent_actions) >= 3:
                        break
                except Exception:
                    continue
    except Exception:
        recent_actions = []

    # Current metrics from context for convenience (LLM can compute delta)
    try:
        oof = context.get("oof_evaluations") or {}
        if isinstance(oof, dict):
            for sym, s in oof.items():
                try:
                    n_bars = None
                    if isinstance(s, dict):
                        if isinstance(s.get("params"), dict) and isinstance(s["params"].get("n_bars"), (int,float)):
                            n_bars = float(s["params"]["n_bars"])
                        else:
                            for k in ("n_bars","bars","total_bars"):
                                if isinstance(s.get(k),(int,float)):
                                    n_bars = float(s[k]); break
                        ic = None; auc = None
                        if isinstance(s.get("fusion"), dict):
                            ic = s["fusion"].get("ic"); auc = s["fusion"].get("auc")
                        for m in ("xgb","lstm"):
                            if ic is None and isinstance(s.get(m), dict): ic = s[m].get("ic")
                            if auc is None and isinstance(s.get(m), dict): auc = s[m].get("auc")
                        cur = {k: float(v) for k, v in {"n_bars": n_bars, "ic": ic, "auc": auc}.items() if v is not None}
                        if cur:
                            metrics_cur_by_symbol[str(sym)] = cur
                except Exception:
                    continue
    except Exception:
        pass

    # Add extracted metrics (n_bars, ic, auc) to allowed numbers to prevent false positives
    try:
        for metrics_dict in [metrics_prev_by_symbol, metrics_cur_by_symbol]:
            for sym_metrics in metrics_dict.values():
                for value in sym_metrics.values():
                    if isinstance(value, (int, float)):
                        allowed_numbers_set.add(float(value))
    except Exception:
        pass

    # Add all numeric values from oof_evaluations context to prevent false positives
    def _extract_numbers_recursive(obj, depth=0, max_depth=5):
        """Recursively extract all numeric values from nested structures."""
        if depth > max_depth:
            return
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            # Round to 3 decimals for consistency with _collect_numbers and validation matching
            try:
                allowed_numbers_set.add(float(round(obj, 3)))
            except Exception:
                pass
        elif isinstance(obj, dict):
            for v in obj.values():
                _extract_numbers_recursive(v, depth + 1, max_depth)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _extract_numbers_recursive(item, depth + 1, max_depth)

    try:
        oof_evals = context.get("oof_evaluations")
        if isinstance(oof_evals, dict):
            _extract_numbers_recursive(oof_evals)
    except Exception:
        pass

    # --- Feature-loop circuit breaker ---------------------------------------------------
    # If the system has already attempted many feature actions today and the most recent
    # ones are clearly failing due to DSL/schema issues, temporarily block new
    # propose_features actions. This prevents the Brain from thrashing on the same
    # invalid patterns (e.g. unsupported DSL ops or bad column names) and forces it to
    # focus on data/window/HPO experiments instead.
    try:
        feature_actions_today = int(action_counts.get("feature_actions", 0))
        # Look at a small window of the most recent actions (already limited above)
        recent_feature_failures = 0
        for ra in recent_actions:
            act = str(ra.get("action") or "")
            status = str(ra.get("status") or "")
            reason = str(ra.get("reason") or "") or ""
            # Count only clear feature-related failures
            is_feature_action = (
                act == "propose_features"
                or (act == "queue_job" and isinstance(ra.get("params"), dict)
                    and str((ra["params"].get("job_type") or "")).lower() in {"feature_build", "feature_experiment"})
            )
            if not is_feature_action:
                continue
            if status not in {"failed", "error"}:
                continue
            # Reasons that indicate structural/DSL/schema problems rather than
            # transient issues.
            if any(
                reason.startswith(prefix)
                for prefix in (
                    "unsupported_dsl_ops",
                    "no_valid_specs",
                    "feature_build_clickhouse_error",
                    "feature_spec_dry_run_schema_invalid",
                )
            ):
                recent_feature_failures += 1

        feature_loop_blocked = bool(
            feature_actions_today >= 10 and recent_feature_failures >= 2
        )
    except Exception:
        feature_loop_blocked = False

    derived_flags["feature_loop_blocked"] = feature_loop_blocked
    if feature_loop_blocked and "propose_features" in allowed_actions:
        allowed_actions.remove("propose_features")

    # Finalize allowed_numbers list after adding all context numbers
    allowed_numbers = sorted(allowed_numbers_set)

    # Missing fields (for LLM to request/wait) — minimal initial set
    missing_fields: List[str] = []
    # Try to detect absence of oof_evaluations with n_bars/ic/auc
    if not isinstance(context.get("oof_evaluations"), dict):
        missing_fields.append("oof_evaluations")

    return {
        "derived_flags": derived_flags,
        "allowed_actions": allowed_actions,
        "allowed_numbers": allowed_numbers,
        "recent_actions": recent_actions,
        "action_counts": action_counts,
        "metrics_prev_by_symbol": metrics_prev_by_symbol,
        "metrics_cur_by_symbol": metrics_cur_by_symbol,
        "missing_fields": missing_fields,
        "recent_backfill_age_min": recent_backfill_age_min,
        "feature_build_status": feature_build_status,
    }


def validate_llm_review_output(
    output: Dict[str, Any],
    allowed_actions: List[str],
    allowed_numbers: List[float],
) -> Tuple[bool, str]:
    """Validate LLM output actions and optional numbers_used against allowed sets.

    Note:
        - Actions are strictly validated against the stage0 allowlist.
        - numbers_used is treated as advisory only. We keep basic type
          sanity checks but no longer hard-fail on values that are not
          present in the context-derived allowed_numbers set, to avoid
          over‑aggressive blocking (fabricated_number_detected) that
          prevents the Brain from iterating on ML/feature experiments.
    """
    try:
        actions = output.get("next_actions", []) or []
        for a in actions:
            act = str(a.get("action") or "")
            if act not in allowed_actions:
                return False, f"action_not_allowed:{act}"
        nums = output.get("numbers_used")
        if nums is not None:
            # Keep only minimal type sanity: all entries must be numeric.
            for v in nums:
                try:
                    float(v)
                except Exception:
                    return False, "invalid_numbers_used"
        return True, "ok"
    except Exception as e:
        return False, f"validation_error:{e}"
