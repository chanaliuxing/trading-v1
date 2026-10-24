"""
Calibrate decision thresholds for intraday option ML signals.

Uses option_intraday_perf to evaluate threshold grids for:
  - theta_dir
  - theta_move
  - min_dir_diff
"""

from __future__ import annotations

import argparse
from typing import List

import pandas as pd

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _fetch_perf_rows(start: str, end: str, model_version: str | None) -> pd.DataFrame:
    client = get_client()
    sql = """
    SELECT
      entry_ts,
      model_version,
      side,
      p_up_entry,
      p_down_entry,
      p_move_entry,
      net_pnl
    FROM trading.option_intraday_perf
    WHERE entry_ts >= toDateTime(%(start)s, 'America/New_York')
      AND entry_ts < toDateTime(%(end)s, 'America/New_York')
      AND exit_ts IS NOT NULL
      AND net_pnl IS NOT NULL
      AND exit_reason != 'OPEN'
    """
    params = {"start": start, "end": end}
    if model_version:
        sql += " AND model_version = %(model_version)s"
        params["model_version"] = model_version
    rows = client.query(sql, parameters=params)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _evaluate_grid(
    df: pd.DataFrame,
    theta_dir_grid: List[float],
    theta_move_grid: List[float],
    min_dir_diff_grid: List[float],
    min_trades: int,
) -> pd.DataFrame:
    rows = []
    total = len(df)
    if total == 0:
        return pd.DataFrame()

    p_up = df["p_up_entry"].astype(float)
    p_down = df["p_down_entry"].astype(float)
    p_move = df["p_move_entry"].astype(float)
    dir_diff = p_up - p_down
    dir_max = pd.concat([p_up, p_down], axis=1).max(axis=1)
    side = df["side"].astype(str)
    net_pnl = df["net_pnl"].astype(float)

    for theta_dir in theta_dir_grid:
        for theta_move in theta_move_grid:
            for min_dir_diff in min_dir_diff_grid:
                eligible = (p_move >= theta_move) & (dir_max >= theta_dir)
                is_call = (dir_diff >= min_dir_diff) & (side == "CALL")
                is_put = (dir_diff <= -min_dir_diff) & (side == "PUT")
                take = eligible & (is_call | is_put)
                trades = int(take.sum())
                if trades < min_trades:
                    continue
                pnl = net_pnl[take]
                hit = (pnl > 0).mean() if trades else 0.0
                avg_pnl = pnl.mean() if trades else 0.0
                coverage = trades / total if total else 0.0
                rows.append(
                    {
                        "theta_dir": theta_dir,
                        "theta_move": theta_move,
                        "min_dir_diff": min_dir_diff,
                        "trades": trades,
                        "hit_rate": float(hit),
                        "avg_pnl": float(avg_pnl),
                        "coverage": float(coverage),
                    }
                )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--model-version", default=None)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--out-csv", default="reports/option_ml_decision_calibration.csv")
    args = parser.parse_args()

    df = _fetch_perf_rows(args.start, args.end, args.model_version)
    if df.empty:
        logger.warning("No perf rows found for calibration", start=args.start, end=args.end)
        return

    theta_dir_grid = [0.1, 0.15, 0.2, 0.25, 0.3]
    theta_move_grid = [0.1, 0.2, 0.3, 0.35, 0.4]
    min_dir_diff_grid = [0.0, 0.01, 0.02, 0.03, 0.05]

    results = _evaluate_grid(
        df,
        theta_dir_grid=theta_dir_grid,
        theta_move_grid=theta_move_grid,
        min_dir_diff_grid=min_dir_diff_grid,
        min_trades=args.min_trades,
    )
    if results.empty:
        logger.warning("No calibration results after filtering", min_trades=args.min_trades)
        return

    results = results.sort_values(
        ["avg_pnl", "hit_rate", "coverage"], ascending=False
    ).reset_index(drop=True)

    results.to_csv(args.out_csv, index=False)
    best = results.iloc[0].to_dict()
    logger.info("Calibration complete", best=best, out_csv=args.out_csv)


if __name__ == "__main__":
    main()
