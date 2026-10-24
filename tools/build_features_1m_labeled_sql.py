"""
tools/build_features_1m_labeled_sql.py

ClickHouse-native label builder using window functions.
Generates labels in trading.features_1m_labeled with optional per-symbol thresholds.

Usage:
  venv/bin/python tools/build_features_1m_labeled_sql.py \
    --start 2025-12-01 --end 2025-12-02 \
    --horizon-min 30 \
    --thresholds-file config/option_ml_thresholds.json \
    --replace
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import os

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)


def _load_thresholds(path: str | None) -> list[tuple[str, float, float]]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        symbols = data.get("symbols", {})
        rows = []
        for sym, vals in symbols.items():
            rows.append((sym, float(vals["ret_threshold"]), float(vals["range_threshold"])))
        return rows
    except FileNotFoundError:
        logger.warning("Thresholds file not found; using empty list", path=path)
    except Exception as exc:
        logger.warning("Failed to load thresholds file", error=str(exc))
    return []


def _build_thresholds_cte(threshold_rows: list[tuple[str, float, float]]) -> str:
    if not threshold_rows:
        return ""
    values_sql = ",\n        ".join(
        f"('{sym}', {ret:.10f}, {rng:.10f})" for sym, ret, rng in threshold_rows
    )
    return f"""
    thresholds AS (
        SELECT * FROM VALUES('symbol String', 'ret_threshold Float64', 'range_threshold Float64')
        {values_sql}
    ),
    """


def build_labels_sql(start: str, end: str, horizon: int, thresholds_file: str | None) -> str:
    threshold_rows = _load_thresholds(thresholds_file)
    thresholds_cte = _build_thresholds_cte(threshold_rows)

    join_thresholds = ""
    ret_thr_expr = "0.0015"
    rng_thr_expr = "0.004"
    if threshold_rows:
        join_thresholds = "LEFT JOIN thresholds t ON t.symbol = b.symbol"
        ret_thr_expr = "coalesce(t.ret_threshold, 0.0015)"
        rng_thr_expr = "coalesce(t.range_threshold, 0.004)"

    sql = f"""
    INSERT INTO trading.features_1m_labeled
    (ts, symbol, horizon_min, ret_h, rng_h, y_dir, y_move, ret_threshold, range_threshold, created_at)
    WITH
    {thresholds_cte}
    base AS (
        SELECT
            b.symbol,
            b.ts_start AS ts,
            b.close,
            b.high,
            b.low,
            lead(b.close, {horizon}) OVER (PARTITION BY b.symbol ORDER BY b.ts_start) AS close_fwd,
            max(b.high) OVER w AS win_high,
            min(b.low) OVER w AS win_low,
            {ret_thr_expr} AS ret_threshold,
            {rng_thr_expr} AS range_threshold
        FROM trading.bars_1m_ibkr b
        {join_thresholds}
        WHERE b.ts_start >= toDateTime('{start}', 'America/New_York')
          AND b.ts_start <  toDateTime('{end}', 'America/New_York')
        WINDOW w AS (
            PARTITION BY b.symbol
            ORDER BY b.ts_start
            ROWS BETWEEN CURRENT ROW AND {horizon} FOLLOWING
        )
    ),
    computed AS (
        SELECT
            symbol,
            ts,
            {horizon} AS horizon_min,
            (close_fwd - close) / close AS ret_h,
            (win_high - win_low) / close AS rng_h,
            ret_threshold,
            range_threshold
        FROM base
        WHERE close_fwd IS NOT NULL
          AND close > 0
    )
    SELECT
        ts,
        symbol,
        horizon_min,
        ret_h,
        rng_h,
        multiIf(ret_h > ret_threshold, 1, ret_h < -ret_threshold, -1, 0) AS y_dir,
        if(rng_h > range_threshold, 1, 0) AS y_move,
        ret_threshold,
        range_threshold,
        now('America/New_York') AS created_at
    FROM computed
    WHERE ret_h IS NOT NULL AND rng_h IS NOT NULL
    """
    return sql


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--horizon-min", type=int, default=30)
    parser.add_argument("--thresholds-file", default="config/option_ml_thresholds.json")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    client = get_client()
    if args.replace:
        client.command(
            """
            ALTER TABLE trading.features_1m_labeled
            DELETE WHERE ts >= toDateTime(%(start)s, 'America/New_York')
              AND ts <  toDateTime(%(end)s, 'America/New_York')
              AND horizon_min = %(horizon)s
            """,
            parameters={"start": args.start, "end": args.end, "horizon": args.horizon_min},
        )

    sql = build_labels_sql(args.start, args.end, args.horizon_min, args.thresholds_file)
    client.command(sql)
    logger.info("Inserted labels via SQL", start=args.start, end=args.end, horizon=args.horizon_min)


if __name__ == "__main__":
    main()
