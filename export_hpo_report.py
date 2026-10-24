#!/usr/bin/env python3
"""
Export HPO Report for AI Brain

从 Optuna 数据库和 completed_trials.jsonl 生成简化报告，供 AI 大脑分析使用。

Usage:
    python export_hpo_report.py \
        --db alpha_optuna_study.db \
        --config alpha_config_enhanced.json \
        --output hpo_report_for_ai.json \
        --top-k 5 \
        --worst-k 3
"""

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime


def load_optuna_trials(db_path: str, study_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """从 Optuna SQLite 数据库加载 trial 结果"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get study_id
    if study_name:
        cursor.execute("SELECT study_id FROM studies WHERE study_name = ?", (study_name,))
    else:
        cursor.execute("SELECT study_id FROM studies ORDER BY study_id DESC LIMIT 1")

    study_row = cursor.fetchone()
    if not study_row:
        raise ValueError(f"No study found in {db_path}")

    study_id = study_row[0]

    # Get all completed trials
    cursor.execute(
        """
        SELECT trial_id, number, value, state, datetime_start, datetime_complete
        FROM trials
        WHERE study_id = ? AND state = 'COMPLETE'
        ORDER BY number
        """,
        (study_id,),
    )

    trials = []
    for row in cursor.fetchall():
        trial_id, number, value, state, dt_start, dt_complete = row

        # Get parameters
        cursor.execute(
            "SELECT param_name, param_value FROM trial_params WHERE trial_id = ?",
            (trial_id,),
        )
        params = {name: val for name, val in cursor.fetchall()}

        # Get user attributes (metrics)
        cursor.execute(
            "SELECT key, value_json FROM trial_user_attributes WHERE trial_id = ?",
            (trial_id,),
        )
        user_attrs = {}
        for key, value_json in cursor.fetchall():
            try:
                user_attrs[key] = json.loads(value_json)
            except:
                user_attrs[key] = value_json

        trials.append(
            {
                "trial_id": trial_id,
                "number": number,
                "value": value,
                "state": state,
                "datetime_start": dt_start,
                "datetime_complete": dt_complete,
                "params": params,
                "user_attrs": user_attrs,
            }
        )

    conn.close()
    return trials


def load_completed_trials_jsonl(jsonl_path: str) -> List[Dict[str, Any]]:
    """从 completed_trials.jsonl 加载详细 metrics"""
    if not Path(jsonl_path).exists():
        return []

    trials = []
    with open(jsonl_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trial = json.loads(line)
                trials.append(trial)
            except json.JSONDecodeError as e:
                print(f"[WARN] Skipping corrupted line: {e}")
                continue

    return trials


def merge_trial_data(
    optuna_trials: List[Dict], jsonl_trials: List[Dict]
) -> List[Dict[str, Any]]:
    """合并 Optuna 和 JSONL 数据"""
    # Build index by params hash
    jsonl_by_params = {}
    for t in jsonl_trials:
        params_str = json.dumps(t.get("params", {}), sort_keys=True)
        if params_str not in jsonl_by_params:
            jsonl_by_params[params_str] = []
        jsonl_by_params[params_str].append(t)

    merged = []
    for ot in optuna_trials:
        params_str = json.dumps(ot["params"], sort_keys=True)

        # Find matching JSONL trials
        matching = jsonl_by_params.get(params_str, [])

        # Extract metrics from user_attrs or JSONL
        metrics = {}
        if "oos_summary" in ot["user_attrs"]:
            summary = ot["user_attrs"]["oos_summary"]
            metrics = {
                "sharpe": summary.get("oos_avg_sharpe", 0.0),
                "ic": summary.get("oos_avg_ic", 0.0),
                "max_drawdown": summary.get("oos_avg_max_drawdown", 0.0),
                "n_trades": summary.get("oos_trades", 0),
                "win_rate": summary.get("oos_avg_win_rate", 0.0),
            }

        # Merge fold-level metrics from JSONL
        fold_sharpes = []
        for jt in matching:
            if jt.get("phase") == "IS":
                fold_sharpes.append(jt.get("results", {}).get("sharpe", 0.0))

        if fold_sharpes:
            metrics["fold_sharpes"] = fold_sharpes

        merged.append(
            {
                "trial_id": ot["trial_id"],
                "number": ot["number"],
                "value": ot["value"],
                "params": ot["params"],
                "metrics": metrics,
            }
        )

    return merged


def generate_report(
    config: Dict[str, Any],
    trials: List[Dict[str, Any]],
    top_k: int = 5,
    worst_k: int = 3,
) -> Dict[str, Any]:
    """生成 AI Brain 可读的报告"""

    # Sort by value (descending for maximize)
    sorted_trials = sorted(trials, key=lambda t: t["value"], reverse=True)

    top_trials = sorted_trials[:top_k]
    worst_trials = sorted_trials[-worst_k:]

    # Compute summary statistics
    values = [t["value"] for t in trials]
    sharpes = [t["metrics"].get("sharpe", 0.0) for t in trials]
    ics = [t["metrics"].get("ic", 0.0) for t in trials]

    best_sharpe = max(sharpes) if sharpes else 0.0
    median_sharpe = sorted(sharpes)[len(sharpes) // 2] if sharpes else 0.0
    worst_sharpe = min(sharpes) if sharpes else 0.0
    best_ic = max(ics) if ics else 0.0

    # IC stability: std of IC across trials
    import statistics

    ic_stability = 1.0 - (statistics.stdev(ics) if len(ics) > 1 else 0.0)

    # Extract metadata
    symbol = config.get("hpo", {}).get("symbols", ["UNKNOWN"])[0]
    start = config.get("hpo", {}).get("start", "")
    end = config.get("hpo", {}).get("end", "")

    # Calculate lookback days (rough estimate)
    try:
        from datetime import datetime

        dt_start = datetime.fromisoformat(start)
        dt_end = datetime.fromisoformat(end)
        lookback_days = (dt_end - dt_start).days
    except:
        lookback_days = 0

    report = {
        "version": "v1",
        "task_meta": {
            "symbol": symbol,
            "lookback_days": lookback_days,
            "data_frequency": f"{config.get('hpo', {}).get('interval_sec', 60)}s",
            "created_at": datetime.now().isoformat(),
        },
        "base_config": {
            "space": {"grid": config.get("optuna_space", {})},
            "label": config.get("label_profile", "unknown"),
            "loss": config.get("loss_function", "mse"),
            "stacking": config.get("stacking", {}),
            "feature_selection": config.get("feature_selection", {}),
        },
        "hard_param_bounds": {
            "max_depth": {"min": 2, "max": 12},
            "learning_rate": {"min": 0.005, "max": 0.5},
            "stack_kfold": {"min": 2, "max": 6},
            "alpha": {"min": 0.0001, "max": 1.0},
            "n_estimators": {"min": 10, "max": 500},
        },
        "last_run_summary": {
            "n_trials": len(trials),
            "best_sharpe": round(best_sharpe, 4),
            "median_sharpe": round(median_sharpe, 4),
            "worst_sharpe": round(worst_sharpe, 4),
            "best_ic": round(best_ic, 4),
            "ic_stability": round(ic_stability, 4),
            "avg_trades_per_day": round(
                sum(t["metrics"].get("n_trades", 0) for t in trials) / len(trials)
                if trials
                else 0,
                1,
            ),
            "max_drawdown": round(
                max(
                    abs(t["metrics"].get("max_drawdown", 0.0)) for t in trials
                ),
                4,
            ),
            "comments": "",  # AI can add notes
        },
        "top_trials": [
            {
                "trial_id": t["trial_id"],
                "number": t["number"],
                "metrics": {
                    k: round(v, 4) if isinstance(v, float) else v
                    for k, v in t["metrics"].items()
                },
                "params": t["params"],
            }
            for t in top_trials
        ],
        "worst_trials": [
            {
                "trial_id": t["trial_id"],
                "number": t["number"],
                "metrics": {
                    k: round(v, 4) if isinstance(v, float) else v
                    for k, v in t["metrics"].items()
                },
                "params": t["params"],
            }
            for t in worst_trials
        ],
    }

    return report


def main():
    parser = argparse.ArgumentParser(description="Export HPO report for AI Brain")
    parser.add_argument(
        "--db", required=True, help="Path to Optuna SQLite database"
    )
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument(
        "--output", default="hpo_report_for_ai.json", help="Output report path"
    )
    parser.add_argument("--top-k", type=int, default=5, help="Number of top trials")
    parser.add_argument(
        "--worst-k", type=int, default=3, help="Number of worst trials"
    )
    parser.add_argument("--study-name", help="Optuna study name (optional)")
    parser.add_argument(
        "--jsonl", default="completed_trials.jsonl", help="JSONL file path (optional)"
    )

    args = parser.parse_args()

    # Load config
    with open(args.config, "r") as f:
        config = json.load(f)

    # Load trials
    print(f"[1/3] Loading trials from {args.db}...")
    optuna_trials = load_optuna_trials(args.db, args.study_name)
    print(f"      Found {len(optuna_trials)} completed trials")

    # Load JSONL (optional)
    jsonl_trials = []
    if Path(args.jsonl).exists():
        print(f"[2/3] Loading detailed metrics from {args.jsonl}...")
        jsonl_trials = load_completed_trials_jsonl(args.jsonl)
        print(f"      Found {len(jsonl_trials)} JSONL entries")

    # Merge
    merged_trials = merge_trial_data(optuna_trials, jsonl_trials)

    # Generate report
    print(f"[3/3] Generating report...")
    report = generate_report(config, merged_trials, args.top_k, args.worst_k)

    # Save
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Report saved to: {args.output}")
    print(f"   Top {args.top_k} trials included")
    print(f"   Worst {args.worst_k} trials included")
    print(f"   Total trials: {len(merged_trials)}")
    print(f"\nNext step: Feed this to AI Brain (ChatGPT/DeepSeek/Claude)")


if __name__ == "__main__":
    main()
