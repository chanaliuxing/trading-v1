#!/usr/bin/env python3
"""
Remove non-RTH rows from feature tables to avoid training on pre/post-market data.

Usage:
  PYTHONPATH=. venv/bin/python tools/clean_rth_features.py \
      --table trading.features_1m \
      --symbols TSLA NVDA \
      --start 2025-11-01 \
      --end 2025-12-02

Defaults:
  - table: trading.features_1m
  - start: 60 days ago
  - end: today
"""
import argparse
from datetime import datetime, timedelta
from typing import List

import clickhouse_connect


def ch_client():
    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        return clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", ""),
        )
    except Exception:
        return clickhouse_connect.get_client(host="localhost", port=8123, database="trading")


def main():
    ap = argparse.ArgumentParser(description="Delete non-RTH rows from feature tables")
    ap.add_argument("--table", default="trading.features_1m", help="Table to clean (e.g., trading.features_1m_with_labels)")
    ap.add_argument("--symbols", nargs="+", default=["TSLA", "NVDA"], help="Symbols to clean")
    ap.add_argument("--start", default=None, help="Start date (YYYY-MM-DD), default 60 days ago")
    ap.add_argument("--end", default=None, help="End date (YYYY-MM-DD), default today")
    args = ap.parse_args()

    end_date = args.end or datetime.now().date().isoformat()
    start_date = args.start or (datetime.now().date() - timedelta(days=60)).isoformat()

    cli = ch_client()
    syms = args.symbols
    tbl = args.table

    # best-effort DELETE (works on MergeTree in recent CH)
    delete_sql = f"""
    DELETE FROM {tbl}
    WHERE symbol IN %(syms)s
      AND ts >= toDateTime(%(start)s)
      AND ts <= toDateTime(%(end)s)
      AND (toHour(ts) < 9 OR toHour(ts) >= 16)
    """
    print(f"[clean] Removing non-RTH rows from {tbl} for {syms} between {start_date} and {end_date}")
    try:
        cli.command(delete_sql, parameters={"syms": syms, "start": f"{start_date} 00:00:00", "end": f"{end_date} 23:59:59"})
        print("[clean] Completed.")
    except Exception as e:
        print(f"[clean] Failed: {e}")


if __name__ == "__main__":
    main()
