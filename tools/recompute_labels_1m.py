#!/usr/bin/env python3
"""
Recompute 1m labels (Python Client Version - Wide Format).

Avoids ClickHouse OOM by processing in Pandas and writes a wide label table
with columns like: label_h5, label_h15, label_h30.

This is designed to integrate with:
  - preflight label checks that expect label_h{h} columns in
    trading.features_1m_with_labels
  - DataHealer.ensure_labels_ready(), which calls this script in batches.

Usage:
  python tools/recompute_labels_1m.py \\
      --symbols TSLA,AAPL \\
      --start "2025-11-01 09:30:00" \\
      --end "2025-11-01 16:00:00" \\
      --horizons 5,15,30 \\
      --table trading.features_1m_with_labels \\
      --source trading.features_1m
"""
from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any, List

import pandas as pd
import clickhouse_connect


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Recompute 1m labels in wide format")
    ap.add_argument("--symbols", required=True, help="Comma-separated symbols, e.g. TSLA,AAPL")
    ap.add_argument("--start", required=True, help="Start timestamp (ISO, e.g. '2025-11-01 09:30:00')")
    ap.add_argument("--end", required=True, help="End timestamp (ISO, e.g. '2025-11-01 16:00:00')")
    ap.add_argument(
        "--horizons",
        default="5,30",
        help="Comma-separated horizons in minutes (e.g. '5,15,30')",
    )
    ap.add_argument(
        "--label-col",
        dest="label_col",
        default=None,
        help="Optional single label column name like label_h5; if provided it overrides --horizons",
    )
    ap.add_argument(
        "--table",
        default="trading.features_1m_with_labels",
        help="Target table to write labels into (default: trading.features_1m_with_labels)",
    )
    ap.add_argument(
        "--source",
        default="trading.features_1m",
        help="Source table for close prices (default: trading.features_1m)",
    )
    return ap.parse_args()


def _connect() -> Any:
    """Connect to ClickHouse using project config when available."""
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


def _ensure_wide_table(cli: Any, table: str, horizons: List[int]) -> None:
    """
    Ensure the wide label table exists with one column per horizon.

    The table schema is:
      ts DateTime,
      symbol String,
      close Float64,
      label_h{h} Nullable(Float64) ...,
      updated_at DateTime DEFAULT now()
    """
    label_cols_def = ", ".join([f"label_h{h} Nullable(Float64)" for h in horizons])
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {table} (
        ts DateTime,
        symbol String,
        close Float64,
        {label_cols_def},
        updated_at DateTime DEFAULT now()
    )
    ENGINE = MergeTree()
    ORDER BY (symbol, ts)
    """
    cli.command(ddl)


def main() -> None:
    args = _parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("No valid symbols provided")
        return

    try:
        # Validate timestamps early
        datetime.fromisoformat(args.start)
        datetime.fromisoformat(args.end)
    except ValueError:
        raise SystemExit("Invalid --start/--end; expected ISO timestamps")

    if args.label_col:
        # Accept label_h{N} or bare integer and derive horizon
        col = args.label_col.strip()
        if col.startswith("label_h"):
            try:
                horizons = [int(col.replace("label_h", ""))]
            except Exception:
                raise SystemExit("Invalid --label-col; expected pattern label_h{int}")
        else:
            try:
                horizons = [int(col)]
            except Exception:
                raise SystemExit("Invalid --label-col; expected pattern label_h{int} or integer minutes")
    else:
        try:
            horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
        except Exception:
            raise SystemExit("Invalid --horizons; expected comma-separated integers (e.g. '5,30')")
    if not horizons:
        raise SystemExit("No valid horizons provided")

    cli = _connect()
    _ensure_wide_table(cli, args.table, horizons)

    for symbol in symbols:
        print(f"[labels] Processing {symbol}...")
        query = f"""
        SELECT ts, close
        FROM {args.source}
        WHERE symbol = %(symbol)s
          AND ts >= toDateTime(%(s)s)
          AND ts <= toDateTime(%(e)s)
          AND toHour(ts) >= 9 AND toHour(ts) < 16
        ORDER BY ts
        """
        # Use query_df to get DataFrame directly
        df = cli.query_df(query, parameters={"symbol": symbol, "s": args.start, "e": args.end})
        if df.empty:
            print(f"[labels]  Query returned empty DataFrame for {symbol}; skipped.")
            continue

        if "ts" not in df.columns:
            print(f"[labels]  No 'ts' column in source result for {symbol}; skipped.")
            continue

        df["ts"] = pd.to_datetime(df["ts"])
        df["symbol"] = symbol

        # Compute labels for each horizon in minutes
        for h in horizons:
            # Shift close forward by h rows (assuming 1m bars)
            fwd = df["close"].shift(-h)

            # ✅ FIX: Safe division with edge case handling
            # Prevent division by zero (close=0) and handle NaN from shift
            import numpy as np
            with np.errstate(divide='ignore', invalid='ignore'):
                lbl = (fwd / df["close"] - 1.0)

            # Replace inf/-inf with NaN (caused by close=0)
            lbl = lbl.replace([np.inf, -np.inf], np.nan)

            # Cast to Float64 (preserves NaN for tail and edge cases)
            df[f"label_h{h}"] = lbl.astype("Float64")

        # Add updated_at
        df["updated_at"] = datetime.utcnow()

        # Prepare for insert: only keep required columns in the correct order
        insert_cols = ["ts", "symbol", "close"] + [f"label_h{h}" for h in horizons] + ["updated_at"]
        df_to_insert = df[insert_cols].copy()

        # Insert into target table; we do not attempt to dedupe here because
        # DataHealer manages temp tables and merge semantics at a higher level.
        cli.insert_df(args.table, df_to_insert, column_names=insert_cols)
        cli.command(f"OPTIMIZE TABLE {args.table} FINAL") # Force merge to apply ReplacingMergeTree logic
        print(f"[labels]  Wrote {len(df_to_insert)} rows for {symbol} into {args.table}")


if __name__ == "__main__":
    main()
