"""
tools/option_ml_structure_selector.py

Select best-performing option structures and optionally update controls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _fetch_structure_perf(start: str, end: str) -> pd.DataFrame:
    client = get_client()
    rows = client.query(
        """
        SELECT
          structure_id,
          count() AS trades,
          avg(net_pnl) AS avg_pnl,
          sum(net_pnl) AS pnl_sum,
          avg(net_pnl > 0) AS win_rate
        FROM trading.option_intraday_perf
        WHERE entry_ts >= toDateTime(%(start)s, 'America/New_York')
          AND entry_ts <  toDateTime(%(end)s,   'America/New_York')
          AND structure_id != ''
        GROUP BY structure_id
        """,
        parameters={"start": start, "end": end},
    )
    return pd.DataFrame(rows)


def _load_controls(strategy_name: str) -> dict:
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
    client.command(
        """
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
        """,
        parameters=row,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--strategy-name", default="intraday_options_long")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--min-win-rate", type=float, default=0.45)
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    df = _fetch_structure_perf(args.start, args.end)
    if df.empty:
        logger.warning("No structure perf rows found", start=args.start, end=args.end)
        return

    df = df[df["trades"] >= args.min_trades]
    df = df[df["win_rate"] >= args.min_win_rate]
    if df.empty:
        logger.warning("No structures pass filters", min_trades=args.min_trades, min_win_rate=args.min_win_rate)
        return

    df = df.sort_values(["avg_pnl", "win_rate", "pnl_sum"], ascending=False)
    selected = df.head(args.top_k)["structure_id"].tolist()
    logger.info("Selected structures", count=len(selected), structures=selected)

    if not args.apply:
        print(json.dumps({"selected": selected}, ensure_ascii=False))
        return

    row = _load_controls(args.strategy_name)
    row["allowed_structures"] = json.dumps(selected, ensure_ascii=False)
    _insert_controls(row)
    logger.info("Updated allowed_structures", count=len(selected))


if __name__ == "__main__":
    main()
