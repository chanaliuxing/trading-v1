"""
Promote an option ML model by inserting a new controls row.

Supports sandbox -> paper -> live transitions via the `mode` field and
sets active_model_version.
"""

from __future__ import annotations

import argparse
import json

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
    parser.add_argument("--strategy-name", default="intraday_options_long")
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--mode", choices=["sandbox", "paper", "live"], required=True)
    parser.add_argument("--enable", choices=["0", "1"], default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    row = _load_latest_controls(args.strategy_name)
    row["active_model_version"] = args.model_version
    row["mode"] = args.mode
    if args.enable is not None:
        row["enabled"] = int(args.enable)

    if args.dry_run:
        logger.info("Controls promotion dry-run", row=json.dumps(row, ensure_ascii=False))
        return

    _insert_controls(row)
    logger.info(
        "Controls promoted",
        strategy_name=args.strategy_name,
        model_version=args.model_version,
        mode=args.mode,
        enabled=row.get("enabled"),
    )


if __name__ == "__main__":
    main()
