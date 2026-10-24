#!/usr/bin/env python3
"""
Runtime health check for QQQ 0DTE option quotes.

Usage:
  python scripts/check_option_quotes_runtime.py --lookback-min 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.db.ch_pool import get_client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", default="QQQ")
    parser.add_argument("--lookback-min", type=int, default=10)
    parser.add_argument("--max-age-sec", type=int, default=90)
    parser.add_argument("--max-dup", type=int, default=1)
    parser.add_argument("--min-rows-per-minute", type=int, default=8)
    args = parser.parse_args()

    ch = get_client()
    u = args.underlying.upper()

    summary = ch.query(
        """
        SELECT
          dateDiff('second', max(ts), now()) AS age_sec,
          count() AS n,
          countIf(ts = toStartOfMinute(ts)) AS exact_ts,
          countIf(ts != toStartOfMinute(ts)) AS non_exact_ts
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND dte = 0
          AND expiry = today()
          AND ts >= now() - INTERVAL {lb:Int32} MINUTE
        """,
        parameters={"u": u, "lb": int(args.lookback_min)},
    )[0]

    dup = ch.query(
        """
        SELECT max(c) AS mx
        FROM (
          SELECT ts, strike, right, count() AS c
          FROM trading.options_quotes_1m
          WHERE underlying = {u:String}
            AND dte = 0
            AND expiry = today()
            AND ts >= now() - INTERVAL {lb:Int32} MINUTE
          GROUP BY ts, strike, right
        )
        """,
        parameters={"u": u, "lb": int(args.lookback_min)},
    )[0]["mx"]

    mins = ch.query(
        """
        SELECT ts, count() AS n
        FROM trading.options_quotes_1m
        WHERE underlying = {u:String}
          AND dte = 0
          AND expiry = today()
          AND ts >= now() - INTERVAL {lb:Int32} MINUTE
        GROUP BY ts
        ORDER BY ts DESC
        """,
        parameters={"u": u, "lb": int(args.lookback_min)},
    )

    print("summary:", summary)
    print("dup_max:", dup)
    print("per_ts_rows:", mins)

    failures = []
    if int(summary["age_sec"]) > int(args.max_age_sec):
        failures.append(f"age_sec>{args.max_age_sec}")
    if int(summary["non_exact_ts"]) > 0:
        failures.append("non_exact_ts>0")
    if int(dup or 0) > int(args.max_dup):
        failures.append(f"dup_max>{args.max_dup}")
    if mins and min(int(r["n"]) for r in mins) < int(args.min_rows_per_minute):
        failures.append(f"rows_per_minute<{args.min_rows_per_minute}")

    if failures:
        print("FAIL:", ", ".join(failures))
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
