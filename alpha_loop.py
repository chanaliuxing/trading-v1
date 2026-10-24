# -*- coding: utf-8 -*-
"""
alpha_loop.py (LLM+ML Cooperative & Optuna)
--------------------------------------------
End-to-end "Chain-of-Alpha" orchestrator with multiple providers:
- llm: LLM proposes grid refinements
- ml: ML surrogate proposes grid via RandomForest
- hybrid: LLM first, fallback to ML
- coop: LLM and ML work simultaneously, merge with origin tracking
- coop_optuna: Two-stage: coop grid generation → Optuna neighborhood refinement
- optuna: Bayesian optimization in continuous/discrete space
- none: Use base grid without refinement

Features:
- Walk-Forward cross-validation with HPO
- Origin tracking for cooperative mode (LLM/ML/BOTH/BASE/MIXED)
- Contribution visualization (proposer_contrib.png, rows_with_origin.csv)
- Grid guardrails via grid_guard.py
- Extended metrics: Sharpe, Sortino, Omega, Calmar, PSR, DSR, PBO

Dependencies:
- wf_hpo.py, backtest_engine.py, sim_adapter.py, grid_guard.py
- Optional: openai (pip install openai) for LLM
- Optional: scikit-learn for ML surrogate
- Optional: optuna (pip install optuna) for Bayesian optimization

Usage:
  python alpha_loop.py --config alpha_config.json --gens 3 --out_dir ./alpha_out --provider coop
  python alpha_loop.py --config alpha_config.json --gens 2 --out_dir ./alpha_out --provider coop_optuna --k-top 12

Config format: see alpha_config_example.json
"""

from __future__ import annotations
print("[HANG-DEBUG] alpha_loop.py: Top-level imports complete.", flush=True)
import os, sys, json, argparse, random, math, ast
from copy import deepcopy
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import boot_logging  # noqa: F401
import numpy as np
import pandas as pd

from wf_hpo import WalkForwardHPO, make_folds
from strategy_registry import StrategyRegistry

# Install extra crash diagnostics as early as possible
try:  # pragma: no cover
    import faulthandler, signal  # type: ignore
    try:
        faulthandler.enable(all_threads=True)
    except Exception:
        pass
    def _sig_handler(signum, frame):
        try:
            print(f"[CRASH-HOOK] Caught signal {signum}; dumping traceback...", flush=True)
            faulthandler.dump_traceback()
        except Exception:
            pass
    for _sig in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGINT', None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _sig_handler)
            except Exception:
                pass
    # Ensure we log normal interpreter exit to detect premature termination
    import atexit as _atexit
    def _on_exit():
        try:
            print("[CRASH-HOOK] Python interpreter exiting (atexit)", flush=True)
        except Exception:
            pass
    _atexit.register(_on_exit)
except Exception:
    pass
from utils.repro import git_commit_sha, config_sha256, dataframe_snapshot
from pi.pi_orchestrator import PrincipalInvestigator
from grid_guard import sanitize_and_constrain_grid
from leakage_guard import enforce_point_in_time
try:
    from libs.checkpoint import CheckpointManager, ClickHouseClient  # type: ignore
except Exception:  # pragma: no cover
    CheckpointManager = None  # type: ignore
    ClickHouseClient = None  # type: ignore

try:  # Optional relay pipeline
    from pipelines.lstm_oof_to_clickhouse import OOFConfig, run_oof_pipeline
except Exception:  # pragma: no cover
    OOFConfig = None  # type: ignore
    run_oof_pipeline = None  # type: ignore

# ---------- Utils ----------

def _ensure_dir(p: str) -> None:
    Path(p).mkdir(parents=True, exist_ok=True)


# Concise progress printing (warnings/errors still go via logger)
def _progress(msg: str, eta: str = "") -> None:
    try:
        ts = time.strftime("%H:%M:%S")
    except Exception:
        ts = ""
    line = f"[Progress {ts}] {msg}"
    if eta:
        line += f" (ETA {eta})"
    print(line)

def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _dump_json(obj: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _sanitize_for_hash(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize_for_hash(v) for k, v in obj.items() if k != "dataset_df"}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_hash(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _repro_meta(cfg: Dict[str, Any], df: Any = None) -> Dict[str, str]:
    sanitized = _sanitize_for_hash(cfg or {})
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    return {
        "git_commit": git_commit_sha(),
        "config_sha256": config_sha256(sanitized),
        "data_snapshot": dataframe_snapshot(df)
    }

def _topk_rows(rows: List[Dict[str, Any]], k: int, key: str) -> List[Dict[str, Any]]:
    rows2 = [r for r in rows if r.get(key) is not None]
    rows2.sort(key=lambda r: r.get(key, 0.0), reverse=True)
    return rows2[:k]

def _expand_numeric(val, scale=0.2):
    try:
        v = float(val)
        lo = v * (1.0 - scale)
        hi = v * (1.0 + scale)
        return [round(lo, 6), round(v, 6), round(hi, 6)]
    except Exception:
        return [val]

def _infer_grid_from_top(rows: List[Dict[str, Any]], param_keys: List[str], widen=0.2) -> Dict[str, List[Any]]:
    # Create a new grid centered around median of top params
    if not rows:
        return {}
    import statistics as st
    params_list = [json.loads(r["params"]) if isinstance(r.get("params"), str) else r["params"] for r in rows]
    out = {}
    for k in param_keys:
        vals = [p.get(k) for p in params_list if k in p]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        # numeric vs categorical
        try:
            nums = [float(v) for v in vals]
            med = st.median(nums)
            out[k] = _expand_numeric(med, scale=widen)
        except Exception:
            # categorical: keep top unique values (cap 5)
            uniq = []
            for v in vals:
                if v not in uniq:
                    uniq.append(v)
            out[k] = uniq[:5]
    return out

# ---------- Safe formula eval ----------

_ALLOWED_NODES = {
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Load, ast.Name,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd,
    ast.Mod, ast.Constant, ast.FloorDiv
}

def _safe_eval_formula(expr: str, ctx: Dict[str, float]) -> float:
    """Safely evaluate arithmetic formula with given context."""
    tree = ast.parse(expr, mode='eval')
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ValueError(f"Unsupported in formula: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in ctx:
            raise ValueError(f"Unknown name: {node.id}")
    return float(eval(compile(tree, "<formula>", "eval"), {"__builtins__": {}}, ctx))

# ---------- LLM glue (optional) ----------

def _llm_available() -> bool:
    try:
        import openai  # type: ignore
        return True
    except Exception:
        return False

def _llm_propose_grid(context: Dict[str, Any], model: str = "gpt-4o-mini", provider: Optional[str] = None) -> Tuple[Dict[str, List[Any]], str]:
    """
    Ask LLM to propose a new grid. Returns (grid_dict, rationale_text).
    If unavailable, returns ({}, "fallback").
    """
    if not _llm_available():
        return ({}, "LLM unavailable; fallback.")
    try:
        from llm_client import ask_for_grid  # enhanced wrapper with multi-provider support
        return ask_for_grid(context, model=model, provider=provider)
    except Exception as e:
        return ({}, f"LLM error: {e}")

# ---------- ML surrogate (optional) ----------

def _ml_available() -> bool:
    try:
        import sklearn  # type: ignore
        return True
    except Exception:
        return False

def _ml_suggest(rows: List[Dict[str, Any]], target_key: str, n: int = 16) -> Tuple[Dict[str, List[Any]], Dict[str, float]]:
    """
    Very light surrogate using RandomForestRegressor (if sklearn available).
    Suggest n points via random search around promising areas.
    Returns (grid dict, feature_importances dict).
    """
    # Parse historical IS rows
    import numpy as np
    import pandas as pd
    try:
        from sklearn.ensemble import RandomForestRegressor  # type: ignore
    except Exception:
        return {}, {}

    if not rows:
        return {}, {}
    df = pd.DataFrame(rows)
    # Expand params JSON
    try:
        pexp = df["params"].apply(lambda s: json.loads(s) if isinstance(s, str) else (s or {}))
    except Exception:
        return {}, {}
    P = pd.json_normalize(pexp)
    if P.empty:
        return {}, {}
    y = df[target_key].astype(float)

    # Encode categoricals (very simple)
    X = P.copy()
    for c in X.columns:
        if not np.issubdtype(X[c].dtype, np.number):
            X[c] = X[c].astype("category").cat.codes

    # Train RF
    try:
        rf = RandomForestRegressor(n_estimators=200, random_state=42)
        rf.fit(X.fillna(0), y)
        importances = {c: float(w) for c, w in zip(X.columns, rf.feature_importances_)}
    except Exception:
        return {}, {}

    # Sample around top rows
    top = _topk_rows(rows, k=min(20, len(rows)), key=target_key)
    top_params = [json.loads(r["params"]) if isinstance(r["params"], str) else r["params"] for r in top]
    candidates = []
    rng = np.random.default_rng(42)
    for _ in range(n * 3):
        cand = {}
        for k in X.columns:
            # derive original values distribution
            col = P[k]
            if np.issubdtype(col.dtype, np.number):
                mu, sd = float(col.mean()), float(col.std() or 1.0)
                cand[k] = float(rng.normal(mu, sd))
            else:
                choices = list(col.dropna().unique())
                cand[k] = rng.choice(choices) if choices else None
        candidates.append(cand)

    # Score candidates
    C = pd.DataFrame(candidates)
    for c in C.columns:
        if not np.issubdtype(C[c].dtype, np.number):
            C[c] = C[c].astype("category").cat.codes
    preds = rf.predict(C.fillna(0))
    C["pred"] = preds
    C = C.sort_values("pred", ascending=False).head(n)

    # Convert top candidates to grid (per-dim unique values)
    grid: Dict[str, List[Any]] = {}
    for _, row in C.iterrows():
        for k, v in row.drop(labels=["pred"]).items():
            grid.setdefault(k, [])
            if v not in grid[k]:
                grid[k].append(v)
    # Try to keep 3-5 choices per key
    for k in grid:
        grid[k] = grid[k][:5]
    return grid, importances

# ---------- Cooperative mode (LLM + ML) with origin tracking ----------

def _merge_grids(grid_base: Dict[str, List[Any]],
                 grid_llm: Dict[str, List[Any]],
                 grid_ml: Dict[str, List[Any]],
                 cap_per_param: int = 5) -> Tuple[Dict[str, List[Any]], Dict[str, Dict[Any, str]]]:
    """
    Merge grids and record origin:
      origin_map[param][value] = "LLM" | "ML" | "BOTH" | "BASE"
    Strategy: BASE ∪ LLM ∪ ML, dedupe and cap at cap_per_param.
    """
    out: Dict[str, List[Any]] = {}
    origin: Dict[str, Dict[Any, str]] = {}

    all_keys = set(grid_base) | set(grid_llm) | set(grid_ml)
    for k in sorted(all_keys):
        vals = []
        src = {}

        def add(vals_in, label):
            for v in (vals_in or []):
                if v not in vals:
                    vals.append(v)
                    src[v] = label
                else:
                    # Upgrade to BOTH if both LLM and ML propose same value
                    if src.get(v) != label and src.get(v) in ("LLM", "ML") and label in ("LLM", "ML"):
                        src[v] = "BOTH"

        add(grid_base.get(k), "BASE")
        add(grid_llm.get(k), "LLM")
        add(grid_ml.get(k), "ML")

        # Cap values
        vals = vals[:cap_per_param]
        out[k] = vals
        # Only keep origin for values that survived the cap
        origin[k] = {v: src[v] for v in vals}
    return out, origin

def _label_row_origin(row_params: Dict[str, Any], origin_map: Dict[str, Dict[Any, str]]) -> str:
    """
    Label a parameter combination by its origin.
    Returns: "LLM" | "ML" | "BOTH" | "MIXED" | "BASE"
    """
    flags = set()
    for k, v in row_params.items():
        src = origin_map.get(k, {}).get(v)
        if src in ("LLM", "ML", "BOTH"):
            flags.add(src)
        else:
            flags.add("BASE")

    # Determine label based on flags
    if flags == {"LLM"}:
        return "LLM"
    if flags == {"ML"}:
        return "ML"
    if flags == {"BASE"}:
        return "BASE"
    if "LLM" in flags and "ML" in flags and len(flags) == 2:
        return "MIXED"
    if "BOTH" in flags and len(flags) == 1:
        return "BOTH"
    return "MIXED"


@dataclass
class RelayConfig:
    ch_host: str = "localhost"
    ch_port: int = 9000
    ch_user: str = "default"
    ch_password: str = ""
    ch_db: str = "trading"

    table_view: str = "features_1m_with_oof"
    symbol: str = "TSLA"
    start: str = "2024-06-01 00:00:00"
    end: str = "2025-10-01 00:00:00"

    label_col: str = "label_h5"
    base_feats: Tuple[str, ...] = ("vwap", "rsi_14", "ema_9", "atr_14", "close")
    relay_feats: Tuple[str, ...] = ("lstm_oof_h5", "lstm_oof_conf_h5")

    n_splits: int = 5
    n_trials: int = 30
    num_round: int = 300
    study_name: str = "relay_optuna"
    pruner_median: bool = True

    max_depth_range: Tuple[int, int] = (3, 8)
    eta_range: Tuple[float, float] = (0.01, 0.2)
    subsample_range: Tuple[float, float] = (0.5, 1.0)
    colsample_range: Tuple[float, float] = (0.5, 1.0)
    min_child_weight_range: Tuple[float, float] = (0.5, 5.0)

    oof_seq_cols: Tuple[str, ...] = ("close", "vwap", "ema_9", "rsi_14", "atr_14")
    oof_epochs: int = 2
    oof_folds: int = 4
    oof_embargo_min: int = 10
    oof_seq_len: int = 60
    oof_hidden: int = 64
    oof_num_layers: int = 1
    oof_dropout: float = 0.1
    oof_model_ver: str = "lstm_h{hidden}_l{layers}_seq{seq}"

def _export_origin_tables_and_plot(rows: List[Dict[str, Any]],
                                   origin_map: Dict[str, Dict[Any, str]],
                                   out_dir: str) -> Dict[str, str]:
    """
    Generate rows_with_origin.csv and proposer_contrib.png
    """
    import pandas as pd
    import matplotlib.pyplot as plt

    paths = {}
    if not rows:
        return paths

    df = pd.DataFrame(rows).copy()
    try:
        df["_params"] = df["params"].apply(lambda s: json.loads(s) if isinstance(s, str) else s)
    except Exception:
        df["_params"] = [{} for _ in range(len(df))]

    df["origin_label"] = df["_params"].apply(lambda d: _label_row_origin(d or {}, origin_map))

    # Export CSV
    p_csv = os.path.join(out_dir, "rows_with_origin.csv")
    df.drop(columns=["_params"], errors="ignore").to_csv(p_csv, index=False)
    paths["rows_with_origin_csv"] = p_csv

    # Contribution plot: count by phase and origin_label
    try:
        plt.figure(figsize=(10, 6))
        counts = df.groupby(["phase", "origin_label"]).size().reset_index(name="cnt")
        phases = sorted(counts["phase"].unique().tolist())
        origins = sorted(counts["origin_label"].unique().tolist())

        for ph in phases:
            sub = counts[counts["phase"] == ph]
            xs = list(range(len(origins)))
            ys = [int(sub[sub["origin_label"] == o]["cnt"].sum()) for o in origins]
            plt.plot(xs, ys, marker="o", label=f"{ph}")

        plt.xticks(list(range(len(origins))), origins, rotation=0)
        plt.xlabel("Origin")
        plt.ylabel("Count")
        plt.title("Proposer Contribution by Phase")
        plt.legend()
        p_png = os.path.join(out_dir, "proposer_contrib.png")
        plt.savefig(p_png, dpi=144, bbox_inches="tight")
        plt.close()
        paths["proposer_contrib_png"] = p_png
    except Exception:
        pass

    return paths

# ---------- Optuna integration ----------

def _optuna_available() -> bool:
    try:
        import optuna  # type: ignore
        return True
    except Exception:
        return False

def _is_number(x) -> bool:
    try:
        float(x)
        return True
    except Exception:
        return False

def _infer_space_from_grid(grid: Dict[str, List[Any]], widen: float = 0.10) -> Dict[str, Dict[str, Any]]:
    """
    Infer search space from existing hpo.grid:
    - Numeric: take min/max and expand by widen on each side
    - Int vs float: check if all values are integers
    - Categorical: use categorical choices
    Returns {param: {"type": "...", ...}}

    Supports two grid formats:
    1. Dict format: {"param": {"type": "float", "low": 0.45, "high": 0.7, "step": 0.05}}
    2. List format: {"param": [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]}
    """
    space: Dict[str, Dict[str, Any]] = {}
    for k, vals in grid.items():
        # ✅ Support dict format (already a space definition)
        if isinstance(vals, dict) and "type" in vals:
            # Convert grid dict format to Optuna space format
            if vals["type"] == "choice" or vals.get("type") == "categorical":
                space[k] = {"type": "categorical", "choices": vals.get("values", vals.get("choices", []))}
            elif vals["type"] == "int":
                space[k] = {
                    "type": "int",
                    "low": int(vals.get("low", 0)),
                    "high": int(vals.get("high", 100)),
                    "step": int(vals.get("step", 1))
                }
            elif vals["type"] == "float":
                space[k] = {
                    "type": "float",
                    "low": float(vals.get("low", 0.0)),
                    "high": float(vals.get("high", 1.0)),
                    "log": bool(vals.get("log", False))
                }
            continue

        # Original list format logic
        if not isinstance(vals, list) or len(vals) == 0:
            continue
        if all(_is_number(v) for v in vals):
            nums = [float(v) for v in vals]
            lo, hi = min(nums), max(nums)
            span = hi - lo
            if span <= 0:
                span = max(abs(lo), 1.0)
            lo2 = lo - span * widen
            hi2 = hi + span * widen
            # Check if integer set
            is_int = all(abs(v - int(v)) < 1e-12 for v in nums)
            if is_int:
                space[k] = {"type": "int", "low": int(math.floor(lo2)), "high": int(math.ceil(hi2)), "step": 1}
            else:
                space[k] = {"type": "float", "low": float(lo2), "high": float(hi2), "log": False}
        else:
            # categorical
            uniq = []
            for v in vals:
                if v not in uniq:
                    uniq.append(v)
            space[k] = {"type": "categorical", "choices": uniq[:16]}
    return space

def _suggest_from_space(trial, space: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Sample parameters from space using Optuna trial."""
    params = {}
    for k, spec in space.items():
        t = spec.get("type")
        if t == "float":
            params[k] = trial.suggest_float(k, spec["low"], spec["high"], log=bool(spec.get("log", False)))
        elif t == "int":
            step = spec.get("step")
            if step and step > 1:
                # Optuna doesn't have step for int, so snap to grid
                raw = trial.suggest_int(k, spec["low"], spec["high"])
                base = spec["low"]
                snapped = base + int(round((raw - base) / step)) * step
                params[k] = max(spec["low"], min(spec["high"], snapped))
            else:
                params[k] = trial.suggest_int(k, spec["low"], spec["high"])
        elif t == "categorical":
            params[k] = trial.suggest_categorical(k, spec["choices"])
        else:
            raise ValueError(f"Unknown space type for {k}: {t}")
    return params

def _score_from_rows_or_summary(rows: List[Dict[str, Any]], summary: Dict[str, Any],
                                objective: Dict[str, Any], target: str) -> float:
    """
    Calculate score from results.
    target: "is" or "oos"
    - If is: compute score from IS phase rows and average
    - If oos: use oos_summary metrics
    Supports both formula (safe eval) and metric.
    """
    metric_key = (objective or {}).get("metric", "sharpe")
    formula = (objective or {}).get("formula")

    def _ctx_from_row(r: Dict[str, Any]) -> Dict[str, float]:
        return {
            "sharpe": float(r.get("sharpe", 0.0)),
            "total_return": float(r.get("total_return", 0.0)),
            "max_drawdown": float(r.get("max_drawdown", 0.0)),
            "win_rate": float(r.get("win_rate", 0.0)),
            "profit_factor": float(r.get("profit_factor", 0.0)),
            "trades": float(r.get("trades", 0.0)),
            "sortino": float(r.get("sortino", 0.0)),
            "omega": float(r.get("omega", 0.0)),
            "calmar": float(r.get("calmar", 0.0)),
        }

    if target == "is":
        is_rows = [r for r in rows if r.get("phase") == "IS"]
        if not is_rows:
            return 0.0
        vals = []
        for r in is_rows:
            if formula:
                vals.append(_safe_eval_formula(formula, _ctx_from_row(r)))
            else:
                vals.append(float(r.get(metric_key, 0.0)))
        return float(sum(vals) / len(vals))
    else:
        # OOS summary
        if formula:
            return _safe_eval_formula(formula, {
                "sharpe": float(summary.get("oos_avg_sharpe", 0.0)),
                "total_return": float(summary.get("oos_avg_total_return", 0.0)),
                "max_drawdown": float(summary.get("oos_avg_max_drawdown", 0.0)),
                "win_rate": float(summary.get("oos_avg_win_rate", 0.0)),
                "profit_factor": float(summary.get("oos_avg_profit_factor", 0.0)),
                "trades": float(summary.get("oos_trades", 0.0)),
                "sortino": float(summary.get("oos_avg_sortino", 0.0)),
                "omega": float(summary.get("oos_avg_omega", 0.0)),
                "calmar": float(summary.get("oos_avg_calmar", 0.0)),
                # ⭐ IC support for formula
                "ic": float(summary.get("oos_avg_ic", 0.0)),
            })
        # metric
        key_map = {
            "sharpe": "oos_avg_sharpe",
            "total_return": "oos_avg_total_return",
            "max_drawdown": "oos_avg_max_drawdown",
            "win_rate": "oos_avg_win_rate",
            "profit_factor": "oos_avg_profit_factor",
            "sortino": "oos_avg_sortino",
            "omega": "oos_avg_omega",
            "calmar": "oos_avg_calmar",
            # ⭐ IC support for metric
            "ic": "oos_avg_ic",
        }
        return float(summary.get(key_map.get(metric_key, "oos_avg_sharpe"), 0.0))

def _run_optuna(cfg: Dict[str, Any], base_hpo_cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run Optuna optimization.
    Returns (best_params, study_summary)
    - Each trial: use suggested params to build single-point grid, run full WalkForwardHPO (IS+OOS),
      return IS/OOS target score (determined by cfg["optuna"]["target"], recommended "is")
    """
    # Explicitly import os to avoid scoping issues
    import os

    if not _optuna_available():
        raise RuntimeError("Optuna not installed. Please `pip install optuna`.")

    # Defensive: cap BLAS/NumExpr threads to prevent runaway CPU/memory
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_MAX_THREADS", "1")

    import optuna

    opt_conf = cfg.get("optuna", {}) or {}
    trials = int(opt_conf.get("trials", 50))
    target = opt_conf.get("target", "is")  # "is" or "oos"
    direction = opt_conf.get("direction", "maximize")
    sampler_name = opt_conf.get("sampler", "tpe").lower()
    study_name = opt_conf.get("study_name", "alpha_optuna")
    # NOTE: For alpha_loop we deliberately avoid RDBStorage to prevent
    # re-loading old trials that may carry incompatible Python objects
    # (e.g., torch models) across versions. We will use in-memory storage
    # by not passing any storage argument to create_study().
    widen = float(opt_conf.get("widen", 0.10))

    # MEMORY FIX: Separate dataset_df from config to avoid deepcopy
    dataset_df = base_hpo_cfg.get("dataset_df", None)
    base_cfg = {k: v for k, v in base_hpo_cfg.items() if k != "dataset_df"}

    # 2) Build search space (prefer optuna_space, else infer from grid)
    hpo_grid = deepcopy(base_cfg.get("grid", {}))
    # Look for optuna_space in full cfg (top-level) first, then in base_cfg (hpo section)
    overrides = (cfg.get("optuna_space") or base_cfg.get("optuna_space") or {})
    if overrides:
        space = overrides
    else:
        space = _infer_space_from_grid(hpo_grid, widen=widen)

    # 2.1) Make study_name stable per-space to avoid Optuna "dynamic value space" errors
    try:
        import hashlib as _hl
        _space_sig = _hl.sha1(json.dumps(space, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
        study_name = f"{study_name}_{_space_sig}"
    except Exception:
        pass

    # 3) Choose sampler
    if sampler_name == "cmaes":
        sampler = optuna.samplers.CmaEsSampler()
    elif sampler_name == "tpe":
        sampler = optuna.samplers.TPESampler(seed=42)
    else:
        sampler = optuna.samplers.TPESampler(seed=42)

    # 4) Configure pruner (optional)
    pruner = None
    pruner_conf = opt_conf.get("pruner", {})
    if pruner_conf.get("enabled", True):  # Default enabled
        n_startup_trials = pruner_conf.get("n_startup_trials", 10)
        n_warmup_steps = pruner_conf.get("n_warmup_steps", 5)
        interval_steps = pruner_conf.get("interval_steps", 1)
        pruner = optuna.pruners.MedianPruner(
            n_startup_trials=n_startup_trials,
            n_warmup_steps=n_warmup_steps,
            interval_steps=interval_steps
        )

    # Persist study only in-memory (no storage=...), so each alpha_loop run starts fresh.
    study = optuna.create_study(direction=direction, sampler=sampler, pruner=pruner,
                                study_name=study_name)
    print(f"[HPO] Study created study={study_name} (in-memory)", flush=True)

    # Optional checkpoint manager (CH + local) - GATED BY CONFIG
    ckpt = None
    try:
        # Gate CheckpointManager by config to avoid hidden I/O stalls
        from libs.config import config as _cfg
        ckpt_enabled = bool(((_cfg.get("policies") or {}).get("ml_checkpoint") or {}).get("enabled", False))
        if ckpt_enabled and CheckpointManager is not None:
            run_id = cfg.get("run_id") or time.strftime("run_%Y%m%d_%H%M%S")
            ch_cfg = ((_cfg.get("policies") or {}).get("ml_checkpoint") or {})
            ch = None
            try:
                if ClickHouseClient is not None:
                    ch = ClickHouseClient(url=ch_cfg.get("url", "http://localhost:8123"),
                                          user=ch_cfg.get("user", ""),
                                          password=ch_cfg.get("password", ""),
                                          database=ch_cfg.get("database", "trading"),
                                          timeout=int(ch_cfg.get("timeout", 5) or 5))
            except Exception as ee:
                print(f"[MEM] ClickHouseClient init failed: {ee}", flush=True)
                ch = None
            ckpt = CheckpointManager(run_id=run_id, study=study_name, ch=ch,
                                     local_dir=os.path.join("runs", study_name))
            print(f"[MEM] CheckpointManager created successfully", flush=True)
        else:
            print("[MEM] CheckpointManager disabled by config", flush=True)
    except Exception as e:
        print(f"[MEM] CheckpointManager failed: {e}", flush=True)
        ckpt = None

    print(f"[MEM] Before defining objective function", flush=True)
    def objective(trial: "optuna.trial.Trial") -> float:
        # Memory tracking
        import psutil
        proc = psutil.Process(os.getpid())
        mem_start = proc.memory_info().rss / 1024 / 1024  # MB
        print(f"[MEM] Trial {trial.number} START: {mem_start:.1f} MB", flush=True)

        # 3.1 Sample parameters
        params = _suggest_from_space(trial, space)  # dict: param -> value
        try:
            mt = params.get("model_type")
            print(f"[HPO-DEBUG] Trial {trial.number} sampled model_type={mt}", flush=True)
        except Exception:
            pass
        if ckpt:
            try:
                ckpt.save(trial_id=trial.number, step="plan", status="in_progress", payload={"params": params})
            except Exception:
                pass

        # 3.2 Run WalkForward with single-point grid (pass trial for pruning)
        # MEMORY FIX: deepcopy the SMALL base_cfg, not the one with the dataset
        cfg1 = deepcopy(base_cfg)
        cfg1["grid"] = {k: [v] for k, v in params.items()}  # single point

        mem_before_runner = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] Before WalkForwardHPO: {mem_before_runner:.1f} MB", flush=True)
        try:
            # Extra breadcrumbs to debug silent crash during __init__
            cfg_keys = sorted(list(cfg1.keys()))
            grid_keys = sorted(list((cfg1.get("grid") or {}).keys()))
            print(
                f"[HPO-DEBUG] Trial {trial.number} constructing WalkForwardHPO "
                f"cfg_keys={cfg_keys} grid_keys={grid_keys}",
                flush=True,
            )
            # MEMORY FIX: runner is now initialized without the large dataset
            runner = WalkForwardHPO(cfg1)
            print(
                f"[HPO-DEBUG] Trial {trial.number} WalkForwardHPO constructed successfully",
                flush=True,
            )
        except BaseException as e:
            # Catch BaseException to log SystemExit/KeyboardInterrupt too
            import traceback
            print(f"[HPO-DEBUG] WalkForwardHPO init failed: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            raise

        mem_after_runner = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] After WalkForwardHPO init: {mem_after_runner:.1f} MB (+{mem_after_runner - mem_before_runner:.1f} MB)", flush=True)

        # MEMORY FIX: Pass dataset_df by reference to the run method
        if dataset_df is not None:
            result = runner.run_with_dataframe(dataset_df, trial=trial)
        else:
            result = runner.run(trial=trial)

        mem_after_run = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] After runner.run(): {mem_after_run:.1f} MB (+{mem_after_run - mem_after_runner:.1f} MB)", flush=True)

        # 3.3 Compute trial score (IS or OOS)
        score = _score_from_rows_or_summary(
            rows=result.get("rows", []),
            summary=result.get("oos_summary", {}),
            objective=cfg1.get("objective", {}),
            target=target
        )

        # Record trial attributes for later retrieval
        trial.set_user_attr("params", params)
        trial.set_user_attr("oos_summary", result.get("oos_summary", {}))
        if ckpt:
            try:
                # Extract oos_summary metrics
                oos_summary = result.get("oos_summary", {}) or {}

                # Extract metadata from cfg (symbol, model_family, etc.)
                symbol = cfg1.get("symbol") or base_cfg.get("symbol") or ""
                model_family = cfg1.get("model_family") or base_cfg.get("model_family") or ""
                horizon = cfg1.get("horizon") or base_cfg.get("horizon") or ""
                window_days = (
                    cfg1.get("window_days")
                    or base_cfg.get("window_days")
                    or cfg1.get("window", {}).get("days")
                    or 0
                )

                # Build rich payload with OOS metrics
                payload = {
                    "score": float(score),
                    "params": params,
                    # Metadata
                    "symbol": symbol,
                    "model_family": model_family,
                    "horizon": horizon,
                    "window_days": int(window_days) if window_days else 0,
                    # OOS metrics (using actual field names from your system)
                    "oos_avg_sharpe": float(oos_summary.get("oos_avg_sharpe", 0.0)),
                    "oos_avg_profit_factor": float(oos_summary.get("oos_avg_profit_factor", 0.0)),
                    "oos_avg_max_drawdown": float(oos_summary.get("oos_avg_max_drawdown", 0.0)),
                    "oos_avg_win_rate": float(oos_summary.get("oos_avg_win_rate", 0.0)),
                    "oos_avg_total_return": float(oos_summary.get("oos_avg_total_return", 0.0)),
                    "oos_trades": int(oos_summary.get("oos_trades", 0)),
                    # ⭐ IC - The key metric for prediction quality
                    "oos_avg_ic": float(oos_summary.get("oos_avg_ic", 0.0)),
                    # Optional: Sortino, Omega, Calmar
                    "oos_avg_sortino": float(oos_summary.get("oos_avg_sortino", 0.0)),
                    "oos_avg_omega": float(oos_summary.get("oos_avg_omega", 0.0)),
                    "oos_avg_calmar": float(oos_summary.get("oos_avg_calmar", 0.0)),
                }

                ckpt.mark_done(trial_id=trial.number, step="trial_end", payload=payload)
                _progress(f"Trial {trial.number}: Saved complete OOF metrics to ClickHouse (IC={payload.get('oos_avg_ic', 0):.4f}, Sharpe={payload.get('oos_avg_sharpe', 0):.4f})")
            except Exception as e:
                _progress(f"WARNING: Failed to save trial {trial.number} to ClickHouse: {e}")

        # Critical: Force memory cleanup after each trial to prevent accumulation
        try:
            del runner
            del result
            del cfg1
        except Exception:
            pass
        import gc
        gc.collect()

        mem_after_cleanup = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] Trial {trial.number} END (after cleanup): {mem_after_cleanup:.1f} MB", flush=True)

        return float(score)

    import psutil, os, sys
    proc_main = psutil.Process(os.getpid())
    mem_before_optimize = proc_main.memory_info().rss / 1024 / 1024
    print(f"[MEM] Before study.optimize(): {mem_before_optimize:.1f} MB", flush=True)

    # Run single-threaded, with GC after trial to reduce memory buildup
    study.optimize(objective, n_trials=trials, show_progress_bar=False, gc_after_trial=True)

    mem_after_optimize = proc_main.memory_info().rss / 1024 / 1024
    print(f"[MEM] After study.optimize(): {mem_after_optimize:.1f} MB", flush=True)

    best = study.best_trial
    best_params = best.user_attrs.get("params", {})
    study_summary = {
        "best_value": float(best.value),
        "best_params": best_params,
        "direction": direction,
        "target": target,
        "trials": trials,
        "oos_summary_of_best": best.user_attrs.get("oos_summary", {})
    }
    return best_params, study_summary

# ---------- Neighbor space builder for coop_optuna ----------

def _center(values: List[Any]) -> float:
    """Calculate median of numeric values."""
    vals = [float(v) for v in values]
    vals.sort()
    n = len(vals)
    if n == 0:
        return 0.0
    return vals[n // 2] if n % 2 == 1 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])

def _build_neighbor_space(merged_grid: Dict[str, List[Any]],
                          champions: List[Dict[str, Any]],
                          scale: float = 0.20) -> Dict[str, Dict[str, Any]]:
    """
    Build Optuna neighborhood search space based on merged grid and champion parameters.
    - Numeric: center around champion median ± scale
    - Categorical: union of champion values and merged grid values
    """
    space: Dict[str, Dict[str, Any]] = {}

    # Collect champion values by parameter
    champ_by_key: Dict[str, List[Any]] = {}
    for c in (champions or []):
        for k, v in (c or {}).items():
            champ_by_key.setdefault(k, []).append(v)

    for k, merged_vals in merged_grid.items():
        if not merged_vals:
            continue

        # Check if numeric
        if all(_is_number(v) for v in merged_vals):
            merged_nums = [float(v) for v in merged_vals]
            lo_m, hi_m = min(merged_nums), max(merged_nums)

            cvals = champ_by_key.get(k, [])
            if cvals and all(_is_number(v) for v in cvals):
                # Use champion center
                ctr = _center(cvals)
                span = max(abs(ctr), (hi_m - lo_m) or 1.0)
                lo = ctr - scale * span
                hi = ctr + scale * span
            else:
                # Fall back to merged grid range
                span = (hi_m - lo_m) or max(abs(lo_m), 1.0)
                lo = lo_m - scale * span
                hi = hi_m + scale * span

            # Clamp to reasonable bounds (allow some expansion beyond merged grid)
            lo = max(lo, lo_m - 2 * abs(hi_m - lo_m))
            hi = min(hi, hi_m + 2 * abs(hi_m - lo_m))

            # Check if integer type
            is_int = all(abs(float(v) - int(float(v))) < 1e-9 for v in merged_nums)
            if is_int:
                space[k] = {"type": "int", "low": int(math.floor(lo)), "high": int(math.ceil(hi)), "step": 1}
            else:
                space[k] = {"type": "float", "low": float(lo), "high": float(hi), "log": False}
        else:
            # Categorical: merge champion choices with grid choices
            choices = []
            for v in champ_by_key.get(k, []):
                if v not in choices:
                    choices.append(v)
            for v in merged_vals:
                if v not in choices:
                    choices.append(v)
            space[k] = {"type": "categorical", "choices": choices[:16]}

    return space

def _run_optuna_with_space(base_hpo_cfg: Dict[str, Any],
                           space: Dict[str, Dict[str, Any]],
                           opt_conf: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run Optuna with explicit space definition.
    Returns (best_params, study_summary).
    """
    if not _optuna_available():
        raise RuntimeError("Optuna not installed. `pip install optuna`.")

    # Defensive: cap BLAS/NumExpr threads to prevent runaway CPU/memory
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_MAX_THREADS", "1")
    import optuna

    trials = int(opt_conf.get("trials", 40))
    target = opt_conf.get("target", "is")  # "is" or "oos"
    direction = opt_conf.get("direction", "maximize")
    sampler_name = opt_conf.get("sampler", "tpe").lower()
    study_name = opt_conf.get("study_name", "alpha_optuna_stage2")

    # MEMORY FIX: Separate dataset from config to avoid repeated deepcopy
    dataset_df = base_hpo_cfg.get("dataset_df", None)
    base_cfg = {k: v for k, v in base_hpo_cfg.items() if k != "dataset_df"}

    if sampler_name == "cmaes":
        sampler = optuna.samplers.CmaEsSampler()
    else:
        sampler = optuna.samplers.TPESampler(seed=42)

    # Configure pruner (optional)
    pruner = None
    pruner_conf = opt_conf.get("pruner", {})
    if pruner_conf.get("enabled", True):  # Default enabled
        n_startup_trials = pruner_conf.get("n_startup_trials", 10)
        n_warmup_steps = pruner_conf.get("n_warmup_steps", 5)
        interval_steps = pruner_conf.get("interval_steps", 1)
        pruner = optuna.pruners.MedianPruner(
            n_startup_trials=n_startup_trials,
            n_warmup_steps=n_warmup_steps,
            interval_steps=interval_steps
        )

    # Use in-memory storage (no storage=...) to avoid reloading incompatible
    # pickled objects from older runs.
    study = optuna.create_study(direction=direction, sampler=sampler, pruner=pruner,
                                study_name=study_name)
    print("[HPO] Study created", study=study_name, flush=True)

    # Optional checkpoint manager (CH + local)
    ckpt = None
    try:
        # Gate CheckpointManager by config to avoid hidden I/O stalls
        from libs.config import config as _cfg
        ckpt_enabled = bool(((_cfg.get("policies") or {}).get("ml_checkpoint") or {}).get("enabled", False))
        if ckpt_enabled and CheckpointManager is not None:
            run_id = opt_conf.get("run_id") or time.strftime("run_%Y%m%d_%H%M%S")
            ch_cfg = ((_cfg.get("policies") or {}).get("ml_checkpoint") or {})
            ch = None
            try:
                if ClickHouseClient is not None:
                    ch = ClickHouseClient(url=ch_cfg.get("url", "http://localhost:8123"),
                                          user=ch_cfg.get("user", ""),
                                          password=ch_cfg.get("password", ""),
                                          database=ch_cfg.get("database", "trading"),
                                          timeout=int(ch_cfg.get("timeout", 5) or 5))
            except Exception as ee:
                print(f"[MEM] ClickHouseClient init failed: {ee}", flush=True)
                ch = None
            ckpt = CheckpointManager(run_id=run_id, study=study_name, ch=ch,
                                     local_dir=os.path.join("runs", study_name))
            print(f"[MEM] CheckpointManager created successfully", flush=True)
        else:
            print("[MEM] CheckpointManager disabled by config", flush=True)
    except Exception as e:
        print(f"[MEM] CheckpointManager failed: {e}", flush=True)
        ckpt = None

    print(f"[MEM] Before defining objective function", flush=True)
    def objective(trial: "optuna.trial.Trial") -> float:
        # Memory tracking
        import psutil
        proc = psutil.Process(os.getpid())
        mem_start = proc.memory_info().rss / 1024 / 1024  # MB
        print(f"[MEM] Trial {trial.number} START: {mem_start:.1f} MB", flush=True)

        # Sample parameters from space
        params = _suggest_from_space(trial, space)
        if ckpt:
            try:
                ckpt.save(trial_id=trial.number, step="plan", status="in_progress", payload={"params": params})
            except Exception:
                pass

        # Run single-point Walk-Forward (pass trial for pruning)
        cfg1 = deepcopy(base_cfg)
        cfg1["grid"] = {k: [v] for k, v in params.items()}

        mem_before_runner = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] Before WalkForwardHPO: {mem_before_runner:.1f} MB", flush=True)
        runner = WalkForwardHPO(cfg1)
        mem_after_runner = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] After WalkForwardHPO init: {mem_after_runner:.1f} MB (+{mem_after_runner - mem_before_runner:.1f} MB)", flush=True)

        # Pass trial to enable intermediate result reporting for pruning
        if dataset_df is not None:
            result = runner.run_with_dataframe(dataset_df, trial=trial)
        else:
            result = runner.run(trial=trial)
        mem_after_run = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] After runner.run(): {mem_after_run:.1f} MB (+{mem_after_run - mem_after_runner:.1f} MB)", flush=True)

        # Calculate score
        score = _score_from_rows_or_summary(
            rows=result.get("rows", []),
            summary=result.get("oos_summary", {}),
            objective=cfg1.get("objective", {}),
            target=target
        )

        trial.set_user_attr("params", params)
        trial.set_user_attr("oos_summary", result.get("oos_summary", {}))
        if ckpt:
            try:
                # Extract oos_summary metrics
                oos_summary = result.get("oos_summary", {}) or {}

                # Extract metadata from cfg
                symbol = cfg1.get("symbol") or base_hpo_cfg.get("symbol") or ""
                model_family = cfg1.get("model_family") or base_hpo_cfg.get("model_family") or ""
                horizon = cfg1.get("horizon") or base_hpo_cfg.get("horizon") or ""
                window_days = (
                    cfg1.get("window_days")
                    or base_hpo_cfg.get("window_days")
                    or cfg1.get("window", {}).get("days")
                    or 0
                )

                # Build rich payload with OOS metrics
                payload = {
                    "score": float(score),
                    "params": params,
                    # Metadata
                    "symbol": symbol,
                    "model_family": model_family,
                    "horizon": horizon,
                    "window_days": int(window_days) if window_days else 0,
                    # OOS metrics (using actual field names from your system)
                    "oos_avg_sharpe": float(oos_summary.get("oos_avg_sharpe", 0.0)),
                    "oos_avg_profit_factor": float(oos_summary.get("oos_avg_profit_factor", 0.0)),
                    "oos_avg_max_drawdown": float(oos_summary.get("oos_avg_max_drawdown", 0.0)),
                    "oos_avg_win_rate": float(oos_summary.get("oos_avg_win_rate", 0.0)),
                    "oos_avg_total_return": float(oos_summary.get("oos_avg_total_return", 0.0)),
                    "oos_trades": int(oos_summary.get("oos_trades", 0)),
                    # ⭐ IC - The key metric for prediction quality
                    "oos_avg_ic": float(oos_summary.get("oos_avg_ic", 0.0)),
                    # Optional: Sortino, Omega, Calmar
                    "oos_avg_sortino": float(oos_summary.get("oos_avg_sortino", 0.0)),
                    "oos_avg_omega": float(oos_summary.get("oos_avg_omega", 0.0)),
                    "oos_avg_calmar": float(oos_summary.get("oos_avg_calmar", 0.0)),
                }

                ckpt.mark_done(trial_id=trial.number, step="trial_end", payload=payload)
                _progress(f"Trial {trial.number}: Saved complete OOF metrics to ClickHouse (IC={payload.get('oos_avg_ic', 0):.4f}, Sharpe={payload.get('oos_avg_sharpe', 0):.4f})")
            except Exception as e:
                _progress(f"WARNING: Failed to save trial {trial.number} to ClickHouse: {e}")

        # Critical: Force memory cleanup after each trial to prevent accumulation
        try:
            del runner
            del result
            del cfg1
        except Exception:
            pass
        import gc
        gc.collect()

        mem_after_cleanup = proc.memory_info().rss / 1024 / 1024
        print(f"[MEM] Trial {trial.number} END (after cleanup): {mem_after_cleanup:.1f} MB", flush=True)

        return float(score)

    import psutil, os, sys
    proc_main = psutil.Process(os.getpid())
    mem_before_optimize = proc_main.memory_info().rss / 1024 / 1024
    print(f"[MEM] Before study.optimize(): {mem_before_optimize:.1f} MB", flush=True)

    study.optimize(objective, n_trials=trials, show_progress_bar=False, gc_after_trial=True)

    mem_after_optimize = proc_main.memory_info().rss / 1024 / 1024
    print(f"[MEM] After study.optimize(): {mem_after_optimize:.1f} MB", flush=True)

    best = study.best_trial
    best_params = best.user_attrs.get("params", {})
    study_summary = {
        "best_value": float(best.value),
        "best_params": best_params,
        "direction": direction,
        "target": target,
        "trials": trials,
        "oos_summary_of_best": best.user_attrs.get("oos_summary", {})
    }
    return best_params, study_summary

def run_relay_optuna(relay_cfg_dict: Dict[str, Any], gen_dir: str) -> Dict[str, Any]:
    if RelayConfig is None:
        raise RuntimeError("RelayConfig unavailable")

    import optuna
    import subprocess
    import time
    import hashlib
    from clickhouse_driver import Client  # type: ignore
    from sklearn.metrics import roc_auc_score, log_loss  # type: ignore
    from sklearn.model_selection import TimeSeriesSplit  # type: ignore
    import xgboost as xgb  # type: ignore

    relay_cfg = RelayConfig(**relay_cfg_dict)
    pruner = optuna.pruners.MedianPruner() if relay_cfg.pruner_median else None
    study = optuna.create_study(direction="maximize", study_name=relay_cfg.study_name, pruner=pruner)

    def ch_client_inner() -> Client:
        return Client(
            host=relay_cfg.ch_host,
            port=relay_cfg.ch_port,
            user=relay_cfg.ch_user,
            password=relay_cfg.ch_password,
            database=relay_cfg.ch_db,
            settings={"use_numpy": True},
        )

    def fetch_frame_inner(use_relay: bool) -> pd.DataFrame:
        import pandas as pd
        cols = ["ts", "symbol", relay_cfg.label_col, *relay_cfg.base_feats]
        if use_relay:
            cols += list(relay_cfg.relay_feats)
        q = f"""
            SELECT {', '.join(cols)}
            FROM {relay_cfg.table_view}
            WHERE symbol = %(sym)s
              AND ts >= %(st)s AND ts < %(ed)s
            ORDER BY ts
        """
        cli = ch_client_inner()
        data = cli.execute(q, {"sym": relay_cfg.symbol, "st": relay_cfg.start, "ed": relay_cfg.end})
        return pd.DataFrame(data, columns=cols).dropna().reset_index(drop=True)

    def maybe_run_oof(params: Dict[str, Any]) -> Optional[str]:
        if not params.get("use_relay", 0):
            return None
        if OOFConfig is None or run_oof_pipeline is None:
            raise RuntimeError("Relay OOF pipeline not available")

        git = subprocess.getoutput("git rev-parse --short HEAD") or "nogit"
        run_id = f"relay_oof_{time.strftime('%Y%m%d_%H%M%S')}_{git}"
        seq_len = int(params.get("oof_seq_len", relay_cfg.oof_seq_len))
        embargo = int(params.get("oof_embargo_min", relay_cfg.oof_embargo_min))
        epochs = int(params.get("oof_epochs", relay_cfg.oof_epochs))
        folds = int(params.get("oof_folds", relay_cfg.oof_folds))
        hidden = int(params.get("oof_hidden", relay_cfg.oof_hidden))
        num_layers = int(params.get("oof_num_layers", relay_cfg.oof_num_layers))
        dropout = float(params.get("oof_dropout", relay_cfg.oof_dropout))
        model_ver = relay_cfg.oof_model_ver.format(hidden=hidden, layers=num_layers, seq=seq_len)

        oof_cfg = OOFConfig(
            ch_host=relay_cfg.ch_host,
            ch_port=relay_cfg.ch_port,
            ch_user=relay_cfg.ch_user,
            ch_password=relay_cfg.ch_password,
            ch_db=relay_cfg.ch_db,
            src_table="features_1m",
            oof_table="oof_lstm_h5",
            symbol=relay_cfg.symbol,
            start=relay_cfg.start,
            end=relay_cfg.end,
            label_col=relay_cfg.label_col,
            seq_cols=relay_cfg.oof_seq_cols,
            seq_len=seq_len,
            folds=folds,
            embargo_min=embargo,
            epochs=epochs,
            hidden=hidden,
            num_layers=num_layers,
            dropout=dropout,
            run_id=run_id,
            model_ver=model_ver,
        )
        print(f"[Relay] generating OOF run_id={run_id}…")
        return run_oof_pipeline(oof_cfg)

    def insert_run(trial: optuna.trial.Trial, run_id: Optional[str], mean_auc: float, aucs: List[float], params: Dict[str, Any]) -> None:
        payload = (
            relay_cfg.study_name,
            trial.number,
            run_id or "",
            relay_cfg.symbol,
            relay_cfg.start,
            relay_cfg.end,
            int(params.get("use_relay", 0)),
            float(mean_auc),
            float(mean_auc),
            [float(x) for x in aucs],
            json.dumps(params, sort_keys=True),
            subprocess.getoutput("git rev-parse --short HEAD") or "nogit",
            hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12],
        )
        q = """
            INSERT INTO trading.runs
            (study, trial_id, run_id, symbol, start, end, use_relay,
             auc, mean_val, fold_scores, params_json, git_commit, config_sha)
            VALUES
        """
        ch_client_inner().execute(q, [payload])

    def objective(trial: optuna.trial.Trial) -> float:
        params: Dict[str, Any] = {
            "use_relay": trial.suggest_categorical("use_relay", [0, 1]),
            "eta": trial.suggest_float("eta", *relay_cfg.eta_range, log=True),
            "max_depth": trial.suggest_int("max_depth", *relay_cfg.max_depth_range),
            "subsample": trial.suggest_float("subsample", *relay_cfg.subsample_range),
            "colsample_bytree": trial.suggest_float("colsample_bytree", *relay_cfg.colsample_range),
            "min_child_weight": trial.suggest_float("min_child_weight", *relay_cfg.min_child_weight_range),
            "oof_seq_len": trial.suggest_int("oof_seq_len", 30, 120),
            "oof_epochs": trial.suggest_int("oof_epochs", 1, 4),
            "oof_folds": trial.suggest_int("oof_folds", 3, 6),
            "oof_embargo_min": trial.suggest_int("oof_embargo_min", 5, 20),
            "oof_hidden": trial.suggest_int("oof_hidden", 32, 128),
            "oof_num_layers": trial.suggest_int("oof_num_layers", 1, 2),
            "oof_dropout": trial.suggest_float("oof_dropout", 0.0, 0.3),
        }

        run_id = maybe_run_oof(params)
        frame = fetch_frame_inner(bool(params["use_relay"]))
        if frame.empty:
            raise optuna.TrialPruned("No data fetched for relay")

        features = list(relay_cfg.base_feats) + (list(relay_cfg.relay_feats) if params["use_relay"] else [])
        X = frame[features].to_numpy(dtype=np.float32)
        y = (frame[relay_cfg.label_col].values > 0).astype(np.int32)

        tscv = TimeSeriesSplit(n_splits=relay_cfg.n_splits)
        aucs: List[float] = []

        xgb_params = {
            "objective": "binary:logistic",
            "eval_metric": "auc",
            "eta": params["eta"],
            "max_depth": int(params["max_depth"]),
            "subsample": params["subsample"],
            "colsample_bytree": params["colsample_bytree"],
            "min_child_weight": params["min_child_weight"],
        }

        for fold_idx, (tr, va) in enumerate(tscv.split(X)):
            dtr = xgb.DMatrix(X[tr], label=y[tr])
            dva = xgb.DMatrix(X[va], label=y[va])
            booster = xgb.train(xgb_params, dtr, relay_cfg.num_round, evals=[(dva, "valid")])
            proba = booster.predict(dva)
            auc = roc_auc_score(y[va], proba)
            aucs.append(float(auc))
            trial.report(auc, step=fold_idx)
            if trial.should_prune():
                raise optuna.TrialPruned(f"Pruned at fold {fold_idx}, auc={auc:.4f}")

        mean_auc = float(np.mean(aucs))
        trial.set_user_attr("aucs", aucs)
        if run_id:
            trial.set_user_attr("oof_run_id", run_id)

        insert_run(trial, run_id, mean_auc, aucs, params)
        return mean_auc

    study.optimize(objective, n_trials=relay_cfg.n_trials, show_progress_bar=False)

    best = study.best_trial
    summary = {
        "study": relay_cfg.study_name,
        "best_value": float(best.value),
        "best_params": best.params,
        "user_attrs": {k: v for k, v in best.user_attrs.items()},
    }
    _ensure_dir(gen_dir)
    _dump_json(summary, os.path.join(gen_dir, "relay_optuna_summary.json"))
    return {"mode": "relay_optuna", "summary": summary}

# ---------- Main loop ----------

def run_loop(cfg: Dict[str, Any], out_dir: str, gens: int, k_top: int, provider: str) -> None:
    print("[HANG-DEBUG] alpha_loop.py: run_loop() started.", flush=True)
    _ensure_dir(out_dir)
    
    # MEMORY FIX: Separate raw config with dataset from the lightweight config used in loops
    raw_hpo_cfg = cfg["hpo"]
    dataset_df = raw_hpo_cfg.get("dataset_df")
    base_hpo_cfg = {k: v for k, v in raw_hpo_cfg.items() if k != "dataset_df"}

    objective_key = base_hpo_cfg.get("objective", {}).get("metric", "sharpe")
    summary_key = "sharpe" if base_hpo_cfg.get("objective", {}).get("formula") is None else "total_return"

    history: List[Dict[str, Any]] = []  # per generation summary

    # Preload dataset once for providers that run many trials, to avoid per-trial reloads.
    if provider in ("optuna", "coop_optuna"):
        print(f"[HANG-DEBUG] alpha_loop.py: Provider is '{provider}'. Starting preload.", flush=True)
        try:
            fold_gen = base_hpo_cfg.get("fold_generator", {}) or {}
            folds = base_hpo_cfg.get("folds", []) or []
            if fold_gen:
                ds_start = fold_gen.get("start")
                ds_end = fold_gen.get("end")
            elif folds:
                ds_start = min(str(f.get("is_start") or f.get("oos_start")) for f in folds)
                ds_end = max(str(f.get("is_end") or f.get("oos_end")) for f in folds)
            else:
                ds_start = None
                ds_end = None

            if ds_start and ds_end and (dataset_df is None):
                adapter = StrategyRegistry.get_adapter(base_hpo_cfg["strategy_path"])
                ch_cfg = cfg.get("clickhouse", {}) or {}
                tbl = base_hpo_cfg.get("features_table")
                timeframe = base_hpo_cfg.get("timeframe", "1m")
                print("[HANG-DEBUG] alpha_loop.py: Calling adapter.load_data().", flush=True)
                df_all = adapter.load_data(
                    symbols=base_hpo_cfg["symbols"],
                    start=ds_start,
                    end=ds_end,
                    clickhouse_config=ch_cfg,
                    features_table=tbl,
                    timeframe=timeframe,
                )
                # Basic validation and light downcast to reduce RSS
                adapter.validate_data(df_all)
                try:
                    num_cols = df_all.select_dtypes(include=["float64","int64"]).columns
                    df_all[num_cols] = df_all[num_cols].apply(pd.to_numeric, downcast="float")
                except Exception:
                    pass

                # Pre-clean the dataframe once to avoid repetition in trials
                df_all["ts"] = pd.to_datetime(df_all["ts"], utc=True, errors="coerce")
                df_all.dropna(subset=["ts"], inplace=True)
                if "feature_cutoff_ts" in df_all.columns:
                    df_all["feature_cutoff_ts"] = pd.to_datetime(
                        df_all["feature_cutoff_ts"], utc=True, errors="coerce"
                    )
                df_all = df_all.sort_values(["ts", "symbol"]).reset_index(drop=True)
                df_all = enforce_point_in_time(df_all, ts_col="ts", cutoff_col="feature_cutoff_ts")

                dataset_df = df_all
                print(f"[MEM] {provider}: preloaded and cleaned dataset rows={len(df_all)} cols={len(df_all.columns)}", flush=True)
        except Exception as e:
            print(f"[MEM] {provider} preload skipped: {e}", flush=True)

    # Add the preloaded (or initially present) dataset to the raw config for passing down
    if dataset_df is not None:
        raw_hpo_cfg["dataset_df"] = dataset_df

    print("[HANG-DEBUG] alpha_loop.py: Entering generations for-loop.", flush=True)
    for g in range(1, gens + 1):
        print(f"[HANG-DEBUG] alpha_loop.py: Starting generation {g}/{gens}.", flush=True)
        _progress(f"Generation {g}/{gens}: start")
        gen_dir = os.path.join(out_dir, f"gen_{g:02d}")
        _ensure_dir(gen_dir)

        if provider == "optuna":
            print("[HANG-DEBUG] alpha_loop.py: Preload complete. Calling _run_optuna().", flush=True)
            # MEMORY FIX: Pass the raw_hpo_cfg which contains the dataset
            best_params, study_summary = _run_optuna(cfg, raw_hpo_cfg)
            
            # Run best params once more to generate artifacts and plots
            # MEMORY FIX: Use the lightweight base_hpo_cfg and inject dataset_df
            hpo_cfg = deepcopy(base_hpo_cfg)
            hpo_cfg["grid"] = {k: [v] for k, v in best_params.items()}
            runner = WalkForwardHPO(hpo_cfg)
            if dataset_df is not None:
                result = runner.run_with_dataframe(dataset_df)
            else:
                result = runner.run()
            artifacts = runner.export_tables(result, gen_dir)
            try:
                plots = runner.export_plots(result, gen_dir)
                artifacts.update(plots)
            except Exception as e:
                artifacts["plot_error"] = str(e)

            meta = _repro_meta(hpo_cfg, dataset_df)
            payload = {
                "gen": g,
                "mode": "optuna",
                "best_params": best_params,
                "study_summary": study_summary,
                "oos_summary": result.get("oos_summary", {}),
                "rows": result.get("rows", []),
                "artifacts": artifacts
            }
            payload.update(meta)
            _dump_json(payload, os.path.join(gen_dir, "gen_summary.json"))
            history.append(payload)
            continue

        if provider == "relay_optuna":
            relay_payload = run_relay_optuna(cfg.get("relay", {}), gen_dir)
            history.append({"gen": g, **relay_payload})
            continue

        if provider == "pi_autopilot":
            hpo_cfg = deepcopy(base_hpo_cfg)
            pi_cfg = cfg.get("pi", {})
            ch_cfg = cfg.get("clickhouse", {})
            llm_cfg = cfg.get("llm", {})
            pi = PrincipalInvestigator(ch_cfg, llm_cfg)

            fold_gen = hpo_cfg.get("fold_generator", {})
            start_ts = pi_cfg.get("start_ts") or fold_gen.get("start") or ch_cfg.get("start")
            end_ts = pi_cfg.get("end_ts") or fold_gen.get("end") or ch_cfg.get("end")
            if not start_ts or not end_ts:
                raise ValueError("pi_autopilot requires start/end timestamps (pi.start_ts/end_ts or fold_generator)")

            table = pi_cfg.get("features_table", hpo_cfg.get("features_table", "features_1m"))
            symbol = pi_cfg.get("symbol") or (hpo_cfg.get("symbols") or [cfg.get("symbol")])[0]
            goal = cfg.get("pi_goal", f"预测 {symbol} 接下来{cfg.get('horizon','5m')} 的对数收益")
            horizon = cfg.get("horizon", "5m")

            pi_res = pi.propose(
                table=table,
                symbol=symbol,
                start_ts=start_ts,
                end_ts=end_ts,
                goal=goal,
                horizon=horizon,
                guard_cfg=pi_cfg.get("guard"),
            )

            hpo_cfg["grid"] = pi_res.hpo_grid
            pi_dataset_df = pi_res.dataset_df
            hpo_cfg.setdefault("symbols", [symbol])
            hpo_cfg.setdefault("model_target_col", pi_res.training_plan.get("target", {}).get("alias", "y"))
            hpo_cfg.setdefault("model_task", pi_res.training_plan.get("task", "reg"))
            hpo_cfg.setdefault("strategy_path", "strategies.model_signal_strategy.ModelSignalStrategy")
            # ... (setting strategy extras) ...

            runner = WalkForwardHPO(hpo_cfg)
            result = runner.run_with_dataframe(pi_dataset_df)
            artifacts = runner.export_tables(result, gen_dir)
            # ... (rest of pi_autopilot logic) ...
            continue

        if provider == "coop":
            _progress(f"Gen {g}: preparing coop context (LLM+ML)", eta="~10-30s")
            hpo_cfg = deepcopy(base_hpo_cfg)
            grid_base = deepcopy(hpo_cfg["grid"])
            # ... (coop logic) ...
            runner = WalkForwardHPO(hpo_cfg)
            if dataset_df is not None:
                result = runner.run_with_dataframe(dataset_df)
            else:
                result = runner.run()
            # ... (coop logic) ...
            continue

        if provider == "coop_optuna":
            _progress(f"Gen {g}: coop stage (LLM/ML refine)", eta="~10-30s")
            hpo_cfg = deepcopy(base_hpo_cfg)
            grid_base = deepcopy(hpo_cfg["grid"])
            # ... (coop_optuna stage 1 logic) ...
            runner1 = WalkForwardHPO(hpo_cfg)
            if dataset_df is not None:
                result1 = runner1.run_with_dataframe(dataset_df)
            else:
                result1 = runner1.run()
            # ... (coop_optuna stage 2 logic) ...
            # MEMORY FIX: Pass the raw_hpo_cfg which contains the dataset
            best_params, study_summary = _run_optuna_with_space(raw_hpo_cfg, space, cfg.get("optuna", {}) or {})
            hpo_final = deepcopy(base_hpo_cfg)
            hpo_final["grid"] = {k: [v] for k, v in best_params.items()}
            runner2 = WalkForwardHPO(hpo_final)
            if dataset_df is not None:
                result2 = runner2.run_with_dataframe(dataset_df)
            else:
                result2 = runner2.run()
            # ... (coop_optuna summary logic) ...
            continue

        # ---- D) Non-Optuna: LLM/ML/Heuristic grid iteration ----
        _progress(f"Gen {g}: prepare base grid", eta="~2-5s")
        hpo_cfg = deepcopy(base_hpo_cfg)
        # ... (grid refinement logic) ...
        runner = WalkForwardHPO(hpo_cfg)
        if dataset_df is not None:
            result = runner.run_with_dataframe(dataset_df)
        else:
            result = runner.run()
        # ... (summary logic) ...

# ---------- CLI ----------

# ---------- ML event helpers (best-effort) ----------
def _runs_dir() -> Path:
    try:
        # Align with AI Trader runs dir if present
        from scripts.ai_trader.orchestrator import Orchestrator  # type: ignore
        o = Orchestrator()
        return Path(o.runs_dir) / date.today().isoformat()
    except Exception:
        return Path("runs/ai_trader") / date.today().isoformat()


def _generate_job_id() -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    import string
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"alpha-{ts}-{rand}"


def _publish_ml_event(event: dict) -> None:
    # Always append to local JSONL for post-mortem analysis
    try:
        d = _runs_dir()
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "ml_events.jsonl"
        with fp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        # Never let logging crash the ML job
        pass

    # Best-effort publish to Kafka (if configured)
    try:
        from libs.config import config as _cfg  # type: ignore
        from libs.event_bus import publish  # type: ignore
        topic = (_cfg.get("event_bus", {}).get("topics", {}) or {}).get("ml", "internal.ml.status")
        publish(topic, event)
    except Exception:
        # Swallow event-bus errors; file log above is the source of truth
        pass

def main():
    print("[HANG-DEBUG] alpha_loop.py: main() started.", flush=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="alpha loop config json")
    ap.add_argument("--out_dir", required=False, default="./alpha_out", help="output directory")
    ap.add_argument("--gens", type=int, default=3, help="number of generations")
    ap.add_argument("--k-top", type=int, default=8, help="top-K IS rows to guide refinement (non-optuna)")
    ap.add_argument("--provider", choices=["llm","ml","hybrid","coop","coop_optuna","optuna","pi_autopilot","none"], default="hybrid", help="who proposes next params")
    args = ap.parse_args()
    print(f"[HANG-DEBUG] alpha_loop.py: Args parsed: {args}", flush=True)

    # Publish ML started event (best-effort)
    job_id = _generate_job_id()
    start_time = time.time()
    _publish_ml_event({
        "event": "ml.job",
        "status": "started",
        "job_id": job_id,
        "job_type": "alpha_loop",
        "params": {"config": args.config, "gens": args.gens, "k_top": args.k_top, "provider": args.provider},
        "ts": datetime.now().isoformat()
    })

    ok = False
    err = None
    try:
        print(f"[HANG-DEBUG] alpha_loop.py: Loading config from {args.config}", flush=True)
        cfg = _load_json(args.config)

        # Apply model space template if specified
        from wf_hpo import apply_model_space_template, load_label_profile
        cfg = apply_model_space_template(cfg)

        # Apply label profile if specified
        cfg = load_label_profile(cfg)

        print("[HANG-DEBUG] alpha_loop.py: Config loaded. Calling run_loop().", flush=True)
        run_loop(cfg, args.out_dir, gens=args.gens, k_top=args.k_top, provider=args.provider)
        ok = True
    except Exception as e:
        ok = False
        err = str(e)
        import traceback
        print(f"[ERROR] Exception in alpha_loop: {err}", flush=True)
        traceback.print_exc()
    finally:
        duration = time.time() - start_time
        # Try to read index.json for OOS summary metrics
        metrics = {}
        try:
            idx = json.loads(open(os.path.join(args.out_dir, "index.json"), "r", encoding="utf-8").read())
            # Prefer stage2 oos_summary if available
            osum = (idx.get("oos_summary") or {})
            # Map common keys
            key_map = {
                "sharpe": "oos_avg_sharpe",
                "profit_factor": "oos_avg_profit_factor",
                "total_return": "oos_avg_total_return",
                "max_drawdown": "oos_avg_max_drawdown",
                "win_rate": "oos_avg_win_rate"
            }
            for k, s in key_map.items():
                if s in osum:
                    try:
                        metrics[k] = float(osum.get(s, 0.0))
                    except Exception:
                        pass
        except Exception:
            pass

        # Publish completed/failed event
        if ok:
            _publish_ml_event({
                "event": "ml.job",
                "status": "completed",
                "job_id": job_id,
                "job_type": "alpha_loop",
                "ok": True,
                "metrics": metrics,
                "duration_sec": round(duration, 2),
                "artifacts": [os.path.join(args.out_dir, "index.json")],
                "ts": datetime.now().isoformat()
            })
        else:
            _publish_ml_event({
                "event": "ml.job",
                "status": "failed",
                "job_id": job_id,
                "job_type": "alpha_loop",
                "error_type": "exception",
                "error_message": err or "unknown",
                "duration_sec": round(duration, 2),
                "ts": datetime.now().isoformat()
            })
        # Return explicit exit code for shell visibility
        return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
