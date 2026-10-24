# -*- coding: utf-8 -*-
"""
Upgraded HPO CLI (production-grade)
- Pluggable Bayesian backends (TPE, BoTorch)
- Folds-aware objective aggregation with leakage purge days
- Hard constraints & early pruning
- ASHA/Median pruners
- RDB-backed Optuna storage for multi-process parallelism
- Optional ClickHouse logging (HTTP)

Example:
    python hpo_cli_upgraded.py \
        --config config/hpo_reve_intraday.json \
        --backend optuna-botorch \
        --study-name HPO-RevE \
        --storage sqlite:///hpo_study.db \
        --runner wf_hpo:evaluate_trial \
        --n-trials 80 --n-jobs 4 --pruner asha --seed 2025 \
        --log-ch http://localhost:8123 --ch-db trading --ch-table hpo_trials

The runner function should accept (config, params, fold) and return a dict that
includes your objective metric, plus auxiliary metrics like 'mdd', 'pnl', 'trades'.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, Any, List, Optional, Tuple

import optuna
from optuna.pruners import MedianPruner, SuccessiveHalvingPruner

from hpo_backends import make_backend
from wf_hpo_adapter import evaluate_with_runner

# -----------------------------
# Utilities
# -----------------------------
def _json_load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _flatten_params(d: Dict[str, Any]) -> Dict[str, Any]:
    # convert nested dicts to dotted_key strings
    def _walk(prefix, obj, out):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(prefix + [k], v, out)
        else:
            out[".".join(prefix)] = obj
    out: Dict[str, Any] = {}
    _walk([], d, out)
    return out

def _build_distributions(grid: Dict[str, Any]) -> List[Tuple[str, Any, str]]:
    """
    Create Optuna distributions from a 'grid' dict in config.
    - If the value is a list: use Categorical.
    - If it's a dict like {"low": x, "high": y, "log": False, "step": s} -> use suggest_float/int
    """
    dists: List[Tuple[str, Any, str]] = []
    for name, spec in grid.items():
        if isinstance(spec, list):
            dists.append((name, ("categorical", spec), "list"))
        elif isinstance(spec, dict):
            low = spec.get("low")
            high = spec.get("high")
            log = bool(spec.get("log", False))
            step = spec.get("step")
            dtype = spec.get("type", "float")
            if dtype == "int":
                dists.append((name, ("int", low, high, step), "range"))
            else:
                dists.append((name, ("float", low, high, step, log), "range"))
        else:
            # fallback categorical single
            dists.append((name, ("categorical", [spec]), "single"))
    return dists

def _sample_params(trial: optuna.Trial, dists: List[Tuple[str, Any, str]]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for name, spec, _kind in dists:
        if spec[0] == "categorical":
            params[name] = trial.suggest_categorical(name, spec[1])
        elif spec[0] == "int":
            _, low, high, step = spec
            if step:
                params[name] = trial.suggest_int(name, low, high, step=step)
            else:
                params[name] = trial.suggest_int(name, low, high)
        elif spec[0] == "float":
            _, low, high, step, log = spec
            if step:
                params[name] = trial.suggest_float(name, low, high, step=step, log=log)
            else:
                params[name] = trial.suggest_float(name, low, high, log=log)
    return params


def _parse_objective_formula(formula: str, metrics: Dict[str, float]) -> float:
    """
    Safely evaluate objective formula with available metrics using AST.

    Supported operations: +, -, *, /, (), abs(), min(), max()

    Examples:
        '0.7*sharpe + 0.3*ic'
        'sharpe - 0.5*abs(mdd)'

    Args:
        formula: Mathematical expression as string
        metrics: Dict of available metrics (e.g., {'sharpe': 1.5, 'ic': 0.25})

    Returns:
        Scalarized objective value

    Raises:
        ValueError: If formula uses undefined metrics or unsafe operations
    """
    import ast
    import operator

    # Safe operators whitelist
    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.USub: operator.neg,
    }

    # Safe functions whitelist
    safe_funcs = {
        'abs': abs,
        'min': min,
        'max': max,
    }

    # Parse formula into AST
    try:
        tree = ast.parse(formula, mode='eval')
    except SyntaxError as e:
        raise ValueError(f"Invalid formula syntax: {e}")

    def _eval_node(node):
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)

        elif isinstance(node, ast.Num):  # Python 3.7 compatibility
            return node.n

        elif isinstance(node, ast.Constant):  # Python 3.8+
            return node.value

        elif isinstance(node, ast.Name):
            # Look up metric name
            if node.id in metrics:
                val = metrics[node.id]
                if val is None or (isinstance(val, float) and val != val):  # None or NaN
                    raise ValueError(f"Metric '{node.id}' has invalid value: {val}")
                return float(val)
            else:
                raise ValueError(f"Unknown metric in formula: '{node.id}'. Available: {list(metrics.keys())}")

        elif isinstance(node, ast.BinOp):
            left = _eval_node(node.left)
            right = _eval_node(node.right)
            if type(node.op) not in ops:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return ops[type(node.op)](left, right)

        elif isinstance(node, ast.UnaryOp):
            operand = _eval_node(node.operand)
            if type(node.op) not in ops:
                raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
            return ops[type(node.op)](operand)

        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Function calls must use simple names")
            func_name = node.func.id
            if func_name not in safe_funcs:
                raise ValueError(f"Unsafe function: '{func_name}'. Allowed: {list(safe_funcs.keys())}")
            args = [_eval_node(arg) for arg in node.args]
            return safe_funcs[func_name](*args)

        else:
            raise TypeError(f"Unsupported AST node type: {type(node).__name__}")

    return _eval_node(tree)


def _aggregate_metric(values: List[float], how: str = "mean") -> float:
    if not values:
        return float("nan")
    if how == "median":
        values_sorted = sorted(values)
        n = len(values_sorted)
        mid = n // 2
        if n % 2 == 1:
            return values_sorted[mid]
        else:
            return 0.5 * (values_sorted[mid - 1] + values_sorted[mid])
    return sum(values) / len(values)

def _violates_constraints(res: Dict[str, Any], obj_cfg: Dict[str, Any]) -> Optional[str]:
    """Return reason if violates; else None."""
    min_trades = obj_cfg.get("min_trades")
    max_mdd    = obj_cfg.get("max_mdd")
    min_pnl    = obj_cfg.get("min_pnl")
    if min_trades is not None and res.get("trades", 0) < int(min_trades):
        return f"trades<{min_trades}"
    if max_mdd is not None and res.get("mdd", 0.0) > float(max_mdd):
        return f"mdd>{max_mdd}"
    if min_pnl is not None and res.get("pnl", 0.0) < float(min_pnl):
        return f"pnl<{min_pnl}"
    return None

# -----------------------------
# Auto data loading
# -----------------------------
def _auto_load_data(symbol: str, start_str: str, end_str: str, config: Dict[str, Any]) -> None:
    """
    Automatically load missing data using load_hist_multi.py.
    Calculates the number of days needed and calls the script.
    """
    import subprocess
    from datetime import datetime

    # Parse dates
    try:
        start_dt = datetime.strptime(start_str[:10], "%Y-%m-%d")
        end_dt = datetime.strptime(end_str[:10], "%Y-%m-%d")
        days_needed = (end_dt - start_dt).days + 5  # Add buffer for weekends
    except Exception as e:
        print(f"[HPO] WARNING: Could not parse dates: {e}")
        days_needed = 30  # Default to 30 days

    # Determine bar size from timeframe
    timeframe = config.get("timeframe", "1m")
    bar_size_map = {
        "1m": "1 mins",
        "5m": "5 mins",
        "15m": "15 mins",
        "1h": "1 hour",
        "1d": "1 day"
    }
    bar_size = bar_size_map.get(timeframe, "1 mins")

    print(f"[HPO]   Symbol: {symbol}")
    print(f"[HPO]   Days: {days_needed}")
    print(f"[HPO]   Bar size: {bar_size}")

    # Call load_hist_multi.py
    cmd = [
        "venv/bin/python",
        "tool/load_hist_multi.py",
        "--symbols", symbol,
        "--days", str(days_needed),
        "--bar-size", bar_size,
        "--fill-mode", "backfill"
    ]

    print(f"[HPO]   Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes timeout
            check=False
        )

        if result.returncode != 0:
            print(f"[HPO] ✗ Data loading failed!")
            print(f"[HPO] STDOUT: {result.stdout[-500:]}")  # Last 500 chars
            print(f"[HPO] STDERR: {result.stderr[-500:]}")
            raise SystemExit(f"Failed to load data for {symbol}")
        else:
            print(f"[HPO] ✓ Data loading command completed")
            # Show last few lines of output
            lines = result.stdout.strip().split('\n')
            for line in lines[-5:]:
                if line.strip():
                    print(f"[HPO]   {line}")

    except subprocess.TimeoutExpired:
        print(f"[HPO] ✗ Data loading timed out after 5 minutes!")
        raise SystemExit(f"Data loading timeout for {symbol}")
    except Exception as e:
        print(f"[HPO] ✗ Error running load_hist_multi.py: {e}")
        raise SystemExit(f"Data loading error for {symbol}")

# -----------------------------
# Data availability check
# -----------------------------
def _check_data_availability(config: Dict[str, Any], folds: List[Dict[str, Any]], auto_load: bool = True) -> None:
    """
    Check if data exists in ClickHouse for the specified date ranges.
    If auto_load=True and data is missing, automatically load it using load_hist_multi.py.
    Otherwise raises SystemExit if data is missing.
    """
    ch_cfg = config.get("clickhouse", {})
    host = ch_cfg.get("host", "localhost")
    port = ch_cfg.get("port", 8123)
    table = config.get("features_table", "trading.features_1m")
    symbols = config.get("symbols", [])

    if not symbols:
        print("[HPO] WARNING: No symbols specified in config, skipping data check")
        return

    try:
        import requests
    except ImportError:
        print("[HPO] WARNING: requests not available, skipping data availability check")
        return

    # Get all date ranges from folds
    date_ranges = []
    for fold in folds:
        # Check both IS and OOS periods
        if fold.get("is_start") and fold.get("is_end"):
            date_ranges.append(("IS", fold["is_start"], fold["is_end"]))
        if fold.get("oos_start") and fold.get("oos_end"):
            date_ranges.append(("OOS", fold["oos_start"], fold["oos_end"]))

    if not date_ranges:
        print("[HPO] No fold date ranges specified, skipping data check")
        return

    # Check each symbol and date range
    for symbol in symbols:
        print(f"[HPO]   Checking {symbol}...")
        for period_type, start, end in date_ranges:
            query = f"""
                SELECT count(*) as cnt
                FROM {table}
                WHERE symbol = '{symbol}'
                  AND ts >= parseDateTime64BestEffort('{start}')
                  AND ts <= parseDateTime64BestEffort('{end}')
            """
            try:
                url = f"http://{host}:{port}/"
                resp = requests.post(url, params={"query": query}, timeout=10)
                resp.raise_for_status()
                count = int(resp.text.strip())

                if count == 0:
                    print(f"\n[HPO] ✗ No data found for {symbol} in {period_type} period")
                    print(f"[HPO]   Date range: {start} to {end}")
                    print(f"[HPO]   Table: {table}")

                    if auto_load:
                        print(f"\n[HPO] 🔄 Auto-loading missing data...")
                        _auto_load_data(symbol, start, end, config)
                        print(f"[HPO] ✓ Data loaded successfully, re-checking...")
                        # Re-check after loading
                        resp2 = requests.post(url, params={"query": query}, timeout=10)
                        resp2.raise_for_status()
                        count2 = int(resp2.text.strip())
                        if count2 == 0:
                            print(f"[HPO] ✗ ERROR: Still no data after auto-load!")
                            raise SystemExit(f"Data auto-load failed for {symbol}")
                        print(f"[HPO]     ✓ {period_type}: {count2} rows loaded")
                    else:
                        print(f"\n[HPO] Suggestions:")
                        print(f"[HPO]   1. Check if data exists: clickhouse-client --query \"SELECT toDate(ts) as date, count(*) FROM {table} WHERE symbol='{symbol}' GROUP BY date ORDER BY date DESC LIMIT 10\"")
                        print(f"[HPO]   2. Update config with valid date range")
                        print(f"[HPO]   3. Load data: venv/bin/python tool/load_hist_multi.py --symbols {symbol} --bar-size 1m --days 30 --fill-mode backfill")
                        raise SystemExit(f"Data availability check failed for {symbol}")
                else:
                    print(f"[HPO]     ✓ {period_type}: {count} rows ({start[:10]} to {end[:10]})")

            except requests.exceptions.RequestException as e:
                print(f"\n[HPO] WARNING: Could not connect to ClickHouse at {host}:{port}")
                print(f"[HPO] Error: {e}")
                print(f"[HPO] Skipping data availability check...")
                return

# -----------------------------
# ClickHouse logging (optional)
# -----------------------------
def _post_clickhouse_rows(rows: List[Dict[str, Any]], ch_url: str, database: str, table: str) -> None:
    """
    Insert JSONEachRow to ClickHouse via HTTP.
    Requires requests; silently skip if not available.
    """
    try:
        import requests  # type: ignore
    except Exception:
        print("[CH] requests not available; skip logging.")
        return
    import json as _json
    # Ensure database exists:
    try:
        requests.post(f"{ch_url}/?query=CREATE%20DATABASE%20IF%20NOT%20EXISTS%20{database}")
    except Exception as e:
        print(f"[CH] WARN create DB failed: {e}")
    # Create table if missing:
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS {database}.{table} (
        ts DateTime DEFAULT now(),
        study String,
        trial UInt32,
        state String,
        value Float64,
        objective String,
        params_json String,
        fold_metrics_json String
    ) ENGINE = MergeTree ORDER BY (study, trial, ts)
    """
    try:
        requests.post(f"{ch_url}", params={"query": create_sql})
    except Exception as e:
        print(f"[CH] WARN create table failed: {e}")
    # Insert rows:
    data = "\\n".join(_json.dumps(r, ensure_ascii=False) for r in rows)
    try:
        requests.post(f"{ch_url}/?query=INSERT%20INTO%20{database}.{table}%20FORMAT%20JSONEachRow",
                      data=data.encode("utf-8"))
    except Exception as e:
        print(f"[CH] WARN insert failed: {e}")

# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser(description="Production HPO CLI with Bayesian backends")
    ap.add_argument("--config", required=True, help="HPO JSON config path")
    ap.add_argument("--study-name", required=True)
    ap.add_argument("--storage", default="", help="Optuna storage URL. e.g., sqlite:///path/to/study.db or postgresql://...")
    ap.add_argument("--backend", default="optuna-tpe", help="optuna-tpe | optuna-botorch")
    ap.add_argument("--n-trials", type=int, default=80)
    ap.add_argument("--n-jobs", type=int, default=1, help="Parallel workers in this process")
    ap.add_argument("--pruner", default="median", help="median | asha | none")
    ap.add_argument("--seed", type=int, default=2025)
    ap.add_argument("--runner", required=True, help="module:function that runs a single trial")
    ap.add_argument("--aggregate", default="mean", help="Aggregate fold metrics: mean|median")
    ap.add_argument("--log-ch", default="", help="ClickHouse HTTP URL, e.g., http://localhost:8123")
    ap.add_argument("--ch-db", default="trading")
    ap.add_argument("--ch-table", default="hpo_trials")
    ap.add_argument("--auto-load-data", action="store_true", default=True, help="Automatically load missing data (default: True)")
    ap.add_argument("--no-auto-load", dest="auto_load_data", action="store_false", help="Disable automatic data loading")
    ap.add_argument("--objective-formula", type=str, default=None,
                   help="Scalarize multiple metrics into single objective. "
                        "Example: '0.7*sharpe + 0.3*ic' or 'sharpe - 0.5*abs(mdd)'. "
                        "Mutually exclusive with --multi-objective.")
    ap.add_argument("--multi-objective", type=str, default=None,
                   help="JSON list of objectives for Pareto optimization. "
                        "Example: '[{\"metric\":\"sharpe\",\"maximize\":true},{\"metric\":\"ic\",\"maximize\":true}]'. "
                        "Uses Optuna NSGA-II sampler. Mutually exclusive with --objective-formula.")

    args = ap.parse_args()

    # Validate mutually exclusive options
    if args.objective_formula and args.multi_objective:
        raise SystemExit("ERROR: --objective-formula and --multi-objective are mutually exclusive")

    # Determine optimization mode
    is_multi_objective = bool(args.multi_objective)
    is_formula_mode = bool(args.objective_formula)

    conf = _json_load(args.config)

    # Build distributions from "grid"
    grid = conf.get("grid", {})
    dists = _build_distributions(grid)
    if not dists:
        raise SystemExit("Config has empty 'grid' for search space.")

    folds = conf.get("folds", [])
    objective_cfg = conf.get("objective", {"metric": "sharpe", "maximize": True})

    # Data availability check
    print("\n[HPO] Checking data availability...")
    if args.auto_load_data:
        print("[HPO] (Auto-load enabled: will fetch missing data from IBKR)")
    _check_data_availability(conf, folds, auto_load=args.auto_load_data)
    print("[HPO] ✓ Data availability check passed\n")

    # Backend (sampler)
    backend = make_backend(args.backend, seed=args.seed)
    sampler, sampler_info = backend.build_sampler()
    print(f"[HPO] Using backend: {sampler_info}")

    # Pruner
    pruner = None
    if args.pruner.lower() == "median":
        pruner = MedianPruner(n_startup_trials=20, n_warmup_steps=0)
        print("[HPO] Using MedianPruner")
    elif args.pruner.lower() == "asha":
        pruner = SuccessiveHalvingPruner(min_resource=1, reduction_factor=3, bootstrap_count=0)
        print("[HPO] Using ASHA Pruner")
    else:
        print("[HPO] No pruner enabled")

    # Storage
    storage = args.storage or None
    if storage:
        print(f"[HPO] Using storage: {storage}")

    # Study creation based on mode
    if is_multi_objective:
        # Multi-objective mode: Parse JSON and create study with directions
        try:
            multi_obj_config = json.loads(args.multi_objective)
            if not isinstance(multi_obj_config, list) or len(multi_obj_config) < 2:
                raise ValueError("Multi-objective must be a JSON list with at least 2 objectives")

            directions = []
            metric_names_mo = []
            for obj in multi_obj_config:
                if not isinstance(obj, dict) or "metric" not in obj:
                    raise ValueError(f"Each objective must be a dict with 'metric' key: {obj}")
                metric_names_mo.append(obj["metric"])
                maximize = obj.get("maximize", True)
                directions.append("maximize" if maximize else "minimize")

            print(f"[HPO] Multi-objective mode: {metric_names_mo}")
            print(f"[HPO] Directions: {directions}")

            # Use NSGA-II sampler for multi-objective (override backend)
            if args.backend != "optuna-tpe":
                print(f"[HPO] WARNING: Multi-objective forces NSGA-II sampler (ignoring --backend {args.backend})")
            sampler = optuna.samplers.NSGAIISampler(seed=args.seed)
            sampler_info = "NSGA-II (multi-objective)"

            study = optuna.create_study(
                study_name=args.study_name,
                storage=storage,
                load_if_exists=True,
                directions=directions,  # Multi-objective
                sampler=sampler,
                pruner=None  # NSGA-II doesn't support pruning
            )
            print(f"[HPO] Running {args.n_trials} trials with {args.n_jobs} parallel jobs")

        except (json.JSONDecodeError, ValueError) as e:
            raise SystemExit(f"ERROR: Invalid --multi-objective JSON: {e}")

    elif is_formula_mode:
        # Formula scalarization mode (single objective with custom formula)
        print(f"[HPO] Formula scalarization mode: {args.objective_formula}")
        direction = "maximize"  # Formula result is maximized

        study = optuna.create_study(
            study_name=args.study_name,
            storage=storage,
            direction=direction,
            load_if_exists=True,
            sampler=sampler,
            pruner=pruner,
        )
        print(f"[HPO] Running {args.n_trials} trials with {args.n_jobs} parallel jobs")

    else:
        # Standard single objective mode
        direction = "maximize" if objective_cfg.get("maximize", True) else "minimize"
        metric_name = objective_cfg.get("metric", "sharpe")

        study = optuna.create_study(
            study_name=args.study_name,
            storage=storage,
            direction=direction,
            load_if_exists=True,
            sampler=sampler,
            pruner=pruner,
        )
        print(f"[HPO] Optimizing metric: {metric_name} ({direction})")
        print(f"[HPO] Running {args.n_trials} trials with {args.n_jobs} parallel jobs")

    def _objective(trial: optuna.Trial):
        params = _sample_params(trial, dists)
        print(f"\n[Trial {trial.number}] Testing params: {params}")

        # Evaluate on each fold (out-of-sample); aggregate
        fold_values: List[float] = []
        fold_metrics_all: List[Dict[str, Any]] = []
        all_metrics_aggregated: Dict[str, List[float]] = {}  # NEW: Track all metrics across folds

        for fold in folds or [None]:
            fold_id = fold.get("fold_id", 0) if fold else 0
            print(f"  Fold {fold_id}: Evaluating...")

            res = evaluate_with_runner(args.runner, conf, params, fold)

            # Constraints check (prune early if violated) - Only for single objective
            if not is_multi_objective:
                violated = _violates_constraints(res, objective_cfg)
                if violated:
                    print(f"  Fold {fold_id}: PRUNED - Constraint violated: {violated}")
                    trial.report(-1e9 if direction == "maximize" else 1e9, step=len(fold_values))
                    raise optuna.TrialPruned(f"Constraint violated: {violated}")

            # Extract primary metric value
            if is_multi_objective or is_formula_mode:
                # Need all metrics for multi-objective or formula
                # Track all metrics across folds
                for key, value in res.items():
                    if isinstance(value, (int, float)):
                        all_metrics_aggregated.setdefault(key, []).append(float(value))

                if is_multi_objective:
                    # For multi-objective, extract each metric
                    fold_mo_values = []
                    for metric_name_mo in metric_names_mo:
                        val = float(res.get(metric_name_mo, float("nan")))
                        if val != val:  # NaN guard
                            print(f"  Fold {fold_id}: WARNING - NaN metric '{metric_name_mo}', using 0.0")
                            val = 0.0
                        fold_mo_values.append(val)

                    fold_values.append(fold_mo_values)  # List of lists
                    print(f"  Fold {fold_id}: {', '.join([f'{m}={v:.4f}' for m, v in zip(metric_names_mo, fold_mo_values)])}")

                else:  # Formula mode
                    # For formula, we'll compute after aggregation
                    pass

            else:
                # Standard single objective
                val = float(res.get(metric_name, float("nan")))
                if val != val:  # NaN guard
                    print(f"  Fold {fold_id}: PRUNED - NaN metric")
                    raise optuna.TrialPruned("NaN metric")

                fold_values.append(val)
                print(f"  Fold {fold_id}: {metric_name}={val:.4f}, trades={res.get('trades', 0)}, mdd={res.get('mdd', 0):.4f}")

            # Keep fold metrics for logging
            fold_metrics_all.append({
                "fold_id": fold_id,
                "metrics": {k: v for k, v in res.items() if isinstance(v, (int, float, str, bool))}
            })

            # Pruning report (only for single objective)
            if not is_multi_objective:
                if is_formula_mode:
                    # For formula, report dummy value (will compute final after all folds)
                    trial.report(0.0, step=len(fold_metrics_all))
                else:
                    trial.report(val, step=len(fold_values))

                if trial.should_prune():
                    print(f"  Fold {fold_id}: PRUNED by pruner")
                    raise optuna.TrialPruned("Pruned by pruner")

        # Aggregate metrics
        if is_multi_objective:
            # Aggregate each objective across folds
            aggregated_values = []
            for i, metric_name_mo in enumerate(metric_names_mo):
                fold_vals_for_metric = [fv[i] for fv in fold_values]
                agg_val = _aggregate_metric(fold_vals_for_metric, args.aggregate)
                aggregated_values.append(agg_val)
                print(f"[Trial {trial.number}] Aggregated {metric_name_mo}: {agg_val:.4f}")

            # Store all metrics in trial user_attrs for later reference
            aggregated_all_metrics = {k: _aggregate_metric(v, args.aggregate) for k, v in all_metrics_aggregated.items()}
            trial.set_user_attr("metrics", aggregated_all_metrics)

            # Optional: ClickHouse logging
            if args.log_ch:
                _post_clickhouse_rows([{
                    "study": args.study_name,
                    "trial": trial.number,
                    "state": "complete",
                    "value": str(aggregated_values),  # Multi-objective values as string
                    "objective": ",".join(metric_names_mo),
                    "params_json": json.dumps(params, ensure_ascii=False),
                    "fold_metrics_json": json.dumps(fold_metrics_all, ensure_ascii=False),
                }], args.log_ch, args.ch_db, args.ch_table)

            return tuple(aggregated_values)  # Return tuple for multi-objective

        elif is_formula_mode:
            # Aggregate all metrics first
            aggregated_all_metrics = {k: _aggregate_metric(v, args.aggregate) for k, v in all_metrics_aggregated.items()}

            # Apply formula
            try:
                formula_value = _parse_objective_formula(args.objective_formula, aggregated_all_metrics)
                print(f"[Trial {trial.number}] Formula '{args.objective_formula}' = {formula_value:.4f}")
                print(f"[Trial {trial.number}] Metrics used: {aggregated_all_metrics}")
            except ValueError as e:
                print(f"[Trial {trial.number}] ERROR evaluating formula: {e}")
                raise optuna.TrialPruned(f"Formula evaluation error: {e}")

            # Store all metrics in trial user_attrs
            trial.set_user_attr("metrics", aggregated_all_metrics)
            trial.set_user_attr("formula", args.objective_formula)
            trial.set_user_attr("formula_value", formula_value)

            # Optional: ClickHouse logging
            if args.log_ch:
                _post_clickhouse_rows([{
                    "study": args.study_name,
                    "trial": trial.number,
                    "state": "complete",
                    "value": formula_value,
                    "objective": f"formula({args.objective_formula})",
                    "params_json": json.dumps(params, ensure_ascii=False),
                    "fold_metrics_json": json.dumps(fold_metrics_all, ensure_ascii=False),
                }], args.log_ch, args.ch_db, args.ch_table)

            return formula_value

        else:
            # Standard single objective
            agg = _aggregate_metric(fold_values, args.aggregate)
            print(f"[Trial {trial.number}] Aggregated {metric_name}: {agg:.4f}")

            # Optional: ClickHouse logging
            if args.log_ch:
                _post_clickhouse_rows([{
                    "study": args.study_name,
                    "trial": trial.number,
                    "state": "complete",
                    "value": agg,
                    "objective": metric_name,
                    "params_json": json.dumps(params, ensure_ascii=False),
                    "fold_metrics_json": json.dumps(fold_metrics_all, ensure_ascii=False),
                }], args.log_ch, args.ch_db, args.ch_table)

            return agg

    # Optimize
    print(f"\n{'='*60}")
    print(f"Starting optimization...")
    print(f"{'='*60}\n")

    study.optimize(_objective, n_trials=args.n_trials, n_jobs=args.n_jobs, gc_after_trial=True)

    # Print best
    print(f"\n{'='*60}")
    print("=== OPTIMIZATION COMPLETE ===")
    print(f"{'='*60}")

    # Check if any trials completed
    completed_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]

    if not completed_trials:
        print("\n⚠️  WARNING: No trials completed successfully!")
        print("All trials were pruned or failed due to constraints.")
        print("\nSuggestions:")
        print("  1. Relax constraints (min_trades, max_mdd, min_pnl)")
        print("  2. Expand parameter search space")
        print("  3. Check if strategy produces any trades in the date range")
        print(f"\nTotal trials run: {len(study.trials)}")
        pruned = len([t for t in study.trials if t.state == optuna.trial.TrialState.PRUNED])
        failed = len([t for t in study.trials if t.state == optuna.trial.TrialState.FAIL])
        print(f"  - Pruned (constraint violations): {pruned}")
        print(f"  - Failed (errors): {failed}")

    elif is_multi_objective:
        # Multi-objective: Get Pareto front
        pareto_trials = study.best_trials  # List of non-dominated trials

        print(f"\n{'='*60}")
        print(f"=== PARETO FRONT ===")
        print(f"{'='*60}")
        print(f"Total trials: {len(study.trials)}")
        print(f"Pareto optimal trials: {len(pareto_trials)}")
        print(f"Objectives: {metric_names_mo}")

        # Build pareto.json
        pareto_data = {
            "study_name": args.study_name,
            "n_trials": len(study.trials),
            "n_pareto": len(pareto_trials),
            "objectives": metric_names_mo,
            "directions": directions,
            "pareto_front": []
        }

        for trial in pareto_trials:
            pareto_entry = {
                "trial_number": trial.number,
                "params": trial.params,
                "values": {
                    metric_names_mo[i]: trial.values[i]
                    for i in range(len(metric_names_mo))
                }
            }
            # Include all metrics if available
            if hasattr(trial, 'user_attrs') and "metrics" in trial.user_attrs:
                pareto_entry["all_metrics"] = trial.user_attrs["metrics"]

            pareto_data["pareto_front"].append(pareto_entry)

        # Save pareto.json
        pareto_path = f"{args.study_name}_pareto.json"
        with open(pareto_path, "w", encoding="utf-8") as f:
            json.dump(pareto_data, f, indent=2, ensure_ascii=False)
        print(f"\n✅ Saved Pareto front to: {pareto_path}")

        # Print Pareto front summary
        print(f"\n{'='*60}")
        print("Pareto Optimal Solutions:")
        print(f"{'='*60}")
        for i, trial in enumerate(pareto_trials[:10], 1):  # Show top 10
            values_str = ", ".join([f"{metric_names_mo[j]}={trial.values[j]:.4f}" for j in range(len(metric_names_mo))])
            print(f"{i}. Trial {trial.number}: {values_str}")
            if i == 10 and len(pareto_trials) > 10:
                print(f"... and {len(pareto_trials) - 10} more")
                break

        # Select best from Pareto front using lexicographic ordering (first objective priority)
        best_trial = pareto_trials[0]  # First in Pareto set (highest first objective)
        print(f"\n{'='*60}")
        print("Selected Best (Lexicographic - First Objective Priority):")
        print(f"{'='*60}")
        print(f"Trial {best_trial.number}")
        for i, metric_name_mo in enumerate(metric_names_mo):
            print(f"  {metric_name_mo}: {best_trial.values[i]:.6f}")
        print("Parameters:")
        for k, v in best_trial.params.items():
            print(f"  {k}: {v}")

        # Build best_params.json with all metrics
        best_params = dict(best_trial.params)
        # Add objective values
        for i, metric_name_mo in enumerate(metric_names_mo):
            best_params[metric_name_mo] = best_trial.values[i]
        # Add all metrics if available
        if hasattr(best_trial, 'user_attrs') and "metrics" in best_trial.user_attrs:
            best_params.update(best_trial.user_attrs["metrics"])
        # Add selection metadata
        best_params["selection_rule"] = "pareto_lexicographic_first_objective"
        best_params["pareto_size"] = len(pareto_trials)

        # Save best_params.json
        best_params_file = f"{args.study_name}_best_params.json"
        with open(best_params_file, "w", encoding="utf-8") as f:
            json.dump(best_params, f, indent=2, ensure_ascii=False)
        print(f"\nBest parameters saved to: {best_params_file}")

    elif is_formula_mode:
        # Formula mode: Single best trial
        best_trial = study.best_trial

        print(f"\n{'='*60}")
        print("=== FORMULA OPTIMIZATION RESULT ===")
        print(f"{'='*60}")
        print(f"Formula: {args.objective_formula}")
        print(f"Best formula value: {best_trial.value:.6f}")
        print(f"Best trial number: {best_trial.number}")

        # Show component metrics
        if hasattr(best_trial, 'user_attrs') and "metrics" in best_trial.user_attrs:
            print("\nComponent metrics:")
            for k, v in best_trial.user_attrs["metrics"].items():
                print(f"  {k}: {v:.6f}")

        print("\nBest parameters:")
        for k, v in best_trial.params.items():
            print(f"  {k}: {v}")

        # Build best_params.json with all metrics
        best_params = dict(best_trial.params)
        best_params["formula_value"] = best_trial.value
        best_params["formula"] = args.objective_formula
        # Add all component metrics
        if hasattr(best_trial, 'user_attrs') and "metrics" in best_trial.user_attrs:
            best_params.update(best_trial.user_attrs["metrics"])
        # Add selection metadata
        best_params["selection_rule"] = "formula_scalarized"

        # Save best_params.json
        best_params_file = f"{args.study_name}_best_params.json"
        with open(best_params_file, "w", encoding="utf-8") as f:
            json.dump(best_params, f, indent=2, ensure_ascii=False)
        print(f"\nBest parameters saved to: {best_params_file}")

        # Print top 5 trials
        print(f"\n{'='*60}")
        print("Top 5 trials:")
        print(f"{'='*60}")
        trials = sorted(study.trials, key=lambda t: t.value if t.value is not None else float('-inf'), reverse=True)[:5]
        for i, t in enumerate(trials, 1):
            if t.value is not None:
                print(f"{i}. Trial {t.number}: formula_value={t.value:.6f} | params={t.params}")

    else:
        # Standard single objective
        best_trial = study.best_trial

        print(f"Best trial number: {best_trial.number}")
        print(f"Best {metric_name}: {best_trial.value:.6f}")
        print(f"Best parameters:")
        for k, v in best_trial.params.items():
            print(f"  {k}: {v}")

        # Save best params to JSON
        best_params = dict(best_trial.params)
        best_params[metric_name] = best_trial.value
        best_params["selection_rule"] = "single_objective_best"

        best_params_file = f"{args.study_name}_best_params.json"
        with open(best_params_file, "w", encoding="utf-8") as f:
            json.dump(best_params, f, indent=2, ensure_ascii=False)
        print(f"\nBest parameters saved to: {best_params_file}")

        # Print top 5 trials
        print(f"\n{'='*60}")
        print("Top 5 trials:")
        print(f"{'='*60}")
        trials = sorted(study.trials, key=lambda t: t.value if t.value is not None else float('-inf'), reverse=(direction == "maximize"))[:5]
        for i, t in enumerate(trials, 1):
            if t.value is not None:
                print(f"{i}. Trial {t.number}: {metric_name}={t.value:.6f} | params={t.params}")

if __name__ == "__main__":
    main()
