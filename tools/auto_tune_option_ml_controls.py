"""
Apply calibrated thresholds to option_ml_controls.

Reads a calibration CSV (from tools/calibrate_option_ml_decision.py) and
inserts a new controls row with updated theta_dir/theta_move/min_dir_diff.
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _load_latest_controls(strategy_name: str) -> dict:
    client = get_client()
    rows = client.query(
        """
        SELECT *
        FROM trading.option_ml_controls
        WHERE strategy_name = %(strategy_name)s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        parameters={"strategy_name": strategy_name},
    )
    if not rows:
        raise RuntimeError(f"No controls found for strategy: {strategy_name}")
    return dict(rows[0])


def _insert_controls(row: dict) -> None:
    client = get_client()
    sql = """
    INSERT INTO trading.option_ml_controls
    (strategy_name, updated_at, enabled, mode, active_model_version,
     theta_dir, theta_move, max_trade_risk_pct, max_daily_dd_pct,
     max_positions_per_symbol, max_total_positions,
     allowed_structures, structure_param_caps_json, max_leg_count, top_n,
     stop_loss_pct_on_premium, take_profit_pct_on_premium, cooldown_min_after_loss,
     max_trades_per_day, max_consecutive_losses, extra_config)
    VALUES
    (%(strategy_name)s, now('America/New_York'), %(enabled)s, %(mode)s, %(active_model_version)s,
     %(theta_dir)s, %(theta_move)s, %(max_trade_risk_pct)s, %(max_daily_dd_pct)s,
     %(max_positions_per_symbol)s, %(max_total_positions)s,
     %(allowed_structures)s, %(structure_param_caps_json)s, %(max_leg_count)s, %(top_n)s,
     %(stop_loss_pct_on_premium)s, %(take_profit_pct_on_premium)s, %(cooldown_min_after_loss)s,
     %(max_trades_per_day)s, %(max_consecutive_losses)s, %(extra_config)s)
    """
    client.command(sql, parameters=row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--strategy-name", default="intraday_options_long")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    if df.empty:
        logger.warning("Calibration CSV is empty", path=args.csv)
        return

    best = df.iloc[0].to_dict()
    row = _load_latest_controls(args.strategy_name)
    row["theta_dir"] = float(best["theta_dir"])
    row["theta_move"] = float(best["theta_move"])

    extra_config = row.get("extra_config") or "{}"
    try:
        extra = json.loads(extra_config)
    except json.JSONDecodeError:
        extra = {}
    extra["min_dir_diff"] = float(best["min_dir_diff"])
    row["extra_config"] = json.dumps(extra)

    if args.dry_run:
        logger.info("Auto-tune dry-run", row=json.dumps(row, ensure_ascii=False, default=str))
        return

    _insert_controls(row)
    logger.info(
        "Auto-tune applied",
        strategy_name=args.strategy_name,
        theta_dir=row["theta_dir"],
        theta_move=row["theta_move"],
        min_dir_diff=extra["min_dir_diff"],
    )


if __name__ == "__main__":
    main()
