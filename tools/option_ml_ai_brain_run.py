"""
tools/option_ml_ai_brain_run.py

Option ML AI Brain runner (automation-first, no LLM prompt required).

Modes:
- training: run data/labels/models/backfill/calibration/controls updates
- live: only run signal service (external) and maintenance/guards
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _run(cmd: list[str]) -> None:
    logger.info("Running step", cmd=" ".join(cmd))
    subprocess.run(cmd, check=True)


def _latest_model_versions() -> tuple[str, str]:
    client = get_client()
    rows = client.query(
        """
        SELECT model_version
        FROM trading.option_ml_models
        ORDER BY created_at DESC
        LIMIT 5
        """
    )
    latest_long = ""
    latest_struct = ""
    for row in rows:
        mv = row["model_version"]
        if not latest_struct and mv.startswith("options_struct_"):
            latest_struct = mv
        if not latest_long and mv.startswith("options_long_"):
            latest_long = mv
    if not latest_long or not latest_struct:
        raise RuntimeError("Could not resolve latest model versions from option_ml_models")
    return latest_long, latest_struct


def _pick_best_model() -> str:
    """Pick the best direction model trained in the last 2 hours."""
    client = get_client()
    # Simple score: 60% AUC, 40% Accuracy
    sql = """
    SELECT model_version
    FROM trading.option_ml_models
    WHERE created_at >= now() - INTERVAL 2 HOUR
      AND model_version LIKE 'options_long_%'
    ORDER BY (val_auc_move * 0.6 + val_accuracy_dir * 0.4) DESC
    LIMIT 1
    """
    rows = client.query(sql)
    if not rows:
        return _latest_model_versions()[0]
    return rows[0]["model_version"]


def _default_range(days: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.date().isoformat(), end.date().isoformat()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["training", "live"], default="training")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--symbols", default="SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA")
    ap.add_argument("--horizon-mins", default="30,60")
    ap.add_argument("--apply-controls", action="store_true")
    ap.add_argument("--skip-labels", action="store_true")
    ap.add_argument("--skip-structure-labels", action="store_true")
    ap.add_argument("--skip-training", action="store_true")
    ap.add_argument("--skip-backfill", action="store_true")
    ap.add_argument("--skip-structure-select", action="store_true")
    ap.add_argument("--structure-min-trades", type=int, default=30)
    ap.add_argument("--structure-min-win-rate", type=float, default=0.45)
    ap.add_argument("--structure-top-k", type=int, default=8)
    ap.add_argument("--structure-strategy-name", default="intraday_options_long")
    ap.add_argument("--skip-calibration", action="store_true")
    ap.add_argument("--auto-model-selection", action="store_true", help="Train multiple models and pick best")
    args = ap.parse_args()

    if not args.start or not args.end:
        start, end = _default_range(args.days)
    else:
        start, end = args.start, args.end

    python = sys.executable

    if args.mode == "training":
        if not args.skip_labels:
            _run([
                python,
                "tools/build_features_1m_labeled.py",
                "--start",
                start,
                "--end",
                end,
                "--symbols",
                args.symbols,
                "--horizon-mins",
                args.horizon_mins,
                "--thresholds-file",
                "config/option_ml_thresholds.json",
                "--replace",
            ])

        if not args.skip_structure_labels:
            _run([
                python,
                "tools/build_structure_pnl_labels.py",
                "--start",
                start,
                "--end",
                end,
                "--symbols",
                args.symbols,
                "--dte",
                "5,7,10,14",
                "--replace",
            ])

        if not args.skip_training:
            models_to_run = ["rf"]
            if args.auto_model_selection:
                models_to_run = ["rf", "xgb", "lgbm", "cat", "lstm", "transformer"]
            
            for m in models_to_run:
                try:
                    _run([
                        python,
                        "ml/train_intraday_long_options.py",
                        "--start",
                        start,
                        "--end",
                        end,
                        "--horizon-min",
                        "30",
                        "--auto-theta",
                        "--model-type",
                        m
                    ])
                except Exception as e:
                    logger.error(f"Training failed for model_type={m}", error=str(e))

            _run([
                python,
                "ml/train_structure_ranker.py",
                "--start",
                start,
                "--end",
                end,
                "--universe",
                args.symbols,
            ])

        if not args.skip_backfill:
            latest_long = _pick_best_model() if args.auto_model_selection else _latest_model_versions()[0]
            latest_struct = _latest_model_versions()[1]
            _run([
                python,
                "tools/backfill_option_intraday_signals.py",
                "--start",
                start,
                "--end",
                end,
                "--model-version",
                latest_long,
                "--structure-model-version",
                latest_struct,
                "--top-n",
                "2",
                "--replace",
            ])
            _run([
                python,
                "tools/backfill_option_intraday_perf.py",
                "--start",
                start,
                "--end",
                end,
                "--equity",
                "100000",
                "--replace",
            ])
            _run([
                python,
                "tools/close_option_intraday_trades.py",
                "--start",
                start,
                "--end",
                end,
                "--max-trades",
                "500",
            ])
            if not args.skip_structure_select:
                _run([
                    python,
                    "tools/option_ml_structure_selector.py",
                    "--start",
                    start,
                    "--end",
                    end,
                    "--strategy-name",
                    args.structure_strategy_name,
                    "--min-trades",
                    str(args.structure_min_trades),
                    "--min-win-rate",
                    str(args.structure_min_win_rate),
                    "--top-k",
                    str(args.structure_top_k),
                    "--apply",
                ])

        if not args.skip_calibration:
            _run([
                python,
                "tools/calibrate_option_ml_decision.py",
                "--start",
                start,
                "--end",
                end,
                "--out-csv",
                "reports/option_ml_decision_calibration.csv",
                "--min-trades",
                "10",
            ])
            if args.apply_controls:
                _run([
                    python,
                    "tools/auto_tune_option_ml_controls.py",
                    "--csv",
                    "reports/option_ml_decision_calibration.csv",
                ])

        _run([
            python,
            "tools/option_ml_daily_maintenance.py",
            "--start",
            start,
            "--end",
            end,
            "--model-version",
            _latest_model_versions()[0],
            "--structure-model-version",
            _latest_model_versions()[1],
            "--equity",
            "100000",
            "--backfill-only",
        ])
        _run([python, "tools/option_ml_degradation_guard.py", "--equity", "100000"])

    else:
        _run([python, "tools/option_ml_daily_maintenance.py", "--start", start, "--end", end,
              "--model-version", _latest_model_versions()[0],
              "--structure-model-version", _latest_model_versions()[1],
              "--equity", "100000"])
        _run([python, "tools/option_ml_degradation_guard.py", "--equity", "100000"])


if __name__ == "__main__":
    main()
