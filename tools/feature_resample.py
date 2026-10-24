#!/usr/bin/env python3
from __future__ import annotations

"""
Utility to build lower-frequency feature tables (e.g., 5m buckets or previous-day aggregates)
from the canonical 1m feature table.
Optimized with chunked processing to prevent ClickHouse OOM.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from datetime import datetime, timedelta

def _cfg_ai_trader() -> Dict[str, Any]:
    """Load ClickHouse DSN from scripts/ai_trader/config/ai_trader.yaml (if present)."""
    p = Path("scripts/ai_trader/config/ai_trader.yaml")
    try:
        import yaml
        return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        return {}

def _ch_client():
    """Return clickhouse_connect client using ai_trader settings."""
    try:
        import clickhouse_connect
    except Exception as e:
        raise RuntimeError(f"clickhouse_connect not installed: {e}")

    cfg = _cfg_ai_trader().get("clickhouse", {}) or {}
    host = "localhost"
    port = 8123
    user = "default"
    password = ""
    database = "trading"
    try:
        dsn = (cfg.get("dsn") or "").strip()
        toks = dsn.split()
        for i, t in enumerate(toks):
            if t == "--host" and i + 1 < len(toks):
                host = toks[i + 1]
            if t == "--port" and i + 1 < len(toks):
                port = int(toks[i + 1])
            if t == "--user" and i + 1 < len(toks):
                user = toks[i + 1]
            if t == "--password" and i + 1 < len(toks):
                password = toks[i + 1]
            if t == "--database" and i + 1 < len(toks):
                database = toks[i + 1]
    except Exception:
        pass
    return clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password, database=database
    )

def _describe_table(cli, table: str) -> List[Tuple[str, str]]:
    """Return (name, type) for all columns in the source table."""
    try:
        res = cli.query(f"DESCRIBE TABLE {table}")
        rows = res.result_rows
    except Exception:
        rows = cli.command(f"DESCRIBE TABLE {table}")
        
    cols: List[Tuple[str, str]] = []
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, (list, tuple)) and len(r) >= 2:
                cols.append((str(r[0]), str(r[1])))
            elif isinstance(r, str):
                parts = [p.strip() for p in r.split("\t") if p.strip()]
                if len(parts) >= 2:
                    cols.append((parts[0], parts[1]))
    elif isinstance(rows, str):
        for line in rows.strip().splitlines():
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if len(parts) >= 2:
                cols.append((parts[0], parts[1]))
    return cols

def _make_select(
    columns: List[Tuple[str, str]], kind: str, interval: int
) -> Tuple[str, str]:
    if kind == "five_min":
        ts_expr = f"toStartOfInterval(ts, INTERVAL {interval} minute)"
    elif kind == "daily_prev":
        ts_expr = "toStartOfDay(ts) + INTERVAL 1 DAY"
    else:
        raise ValueError(f"Unknown kind '{kind}'")

    select_parts = [
        "symbol",
        f"{ts_expr} AS ts_bucket",
    ]

    sum_cols = {"volume", "turnover", "notional"}
    for name, _ctype in columns:
        if name in {"symbol", "ts"}:
            continue
        quoted_name = f"`{name}`"
        agg = "sum" if name in sum_cols else "argMax"
        if agg == "sum":
            select_parts.append(f"sum({quoted_name}) AS {quoted_name}")
        else:
            select_parts.append(f"argMax({quoted_name}, ts) AS {quoted_name}")

    select_sql = ",\n        ".join(select_parts)
    return select_sql, ts_expr

def _get_data_range(cli, table: str) -> Tuple[datetime, datetime]:
    """Get min and max timestamp from the table."""
    try:
        res = cli.query(f"SELECT min(ts), max(ts) FROM {table}")
        row = res.result_rows[0]
        min_ts, max_ts = row[0], row[1]
        # Ensure we have datetimes
        if not min_ts: min_ts = datetime.now() - timedelta(days=30)
        if not max_ts: max_ts = datetime.now()
        return min_ts, max_ts
    except Exception:
        # Fallback
        return datetime.now() - timedelta(days=30), datetime.now()

def main() -> None:
    ap = argparse.ArgumentParser(description="Resample features_1m into coarser tables.")
    ap.add_argument("--source", default="trading.features_1m", help="Source ClickHouse table")
    ap.add_argument("--target", required=True, help="Target table to create/replace")
    ap.add_argument("--kind", choices={"five_min", "daily_prev"}, required=True)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    ap.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    ap.add_argument("--chunk-days", type=int, default=10, help="Days per chunk to process")

    args = ap.parse_args()
    cli = _ch_client()

    cols = _describe_table(cli, args.source)
    if not cols:
        raise RuntimeError(f"Failed to DESCRIBE {args.source}")

    select_sql, ts_expr = _make_select(cols, args.kind, args.interval)

    if args.replace:
        try:
            cli.command(f"DROP TABLE IF EXISTS {args.target}")
        except Exception:
            pass

    # Create target table
    ddl_cols = [f"`{name}` {ctype}" for name, ctype in cols if name not in {'symbol', 'ts'}]
    
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {args.target}
    (
        symbol String,
        ts DateTime,
        {', '.join(ddl_cols)}
    )
    ENGINE = MergeTree
    ORDER BY (symbol, ts)
    """
    cli.command(ddl)

    # Determine Time Range
    if args.start_date:
        start_dt = datetime.strptime(args.start_date, "%Y-%m-%d")
    else:
        # If replacing, we need full history; if not, maybe just recent? 
        # Assuming full history if start not specified for safety/completeness in this tool
        db_min, _ = _get_data_range(cli, args.source)
        start_dt = db_min

    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d") + timedelta(days=1) # Inclusive
    else:
        _, db_max = _get_data_range(cli, args.source)
        end_dt = db_max + timedelta(days=1) # Ensure we cover the last day

    # Chunked Processing
    total_days = (end_dt - start_dt).days + 1
    chunk_size = timedelta(days=args.chunk_days)
    
    current_start = start_dt
    
    print(f"[Info] Processing range: {start_dt} to {end_dt} in {args.chunk_days} day chunks.")

    # Memory settings: allow spill to disk
    settings = "SETTINGS max_bytes_before_external_group_by = 4000000000, max_memory_usage = 12000000000"

    while current_start < end_dt:
        current_end = min(current_start + chunk_size, end_dt)
        
        query = f"""
        INSERT INTO {args.target}
        SELECT
            {select_sql}
        FROM {args.source}
        WHERE ts >= '{current_start.strftime("%Y-%m-%d %H:%M:%S")}' 
          AND ts < '{current_end.strftime("%Y-%m-%d %H:%M:%S")}'
        GROUP BY
            symbol,
            {ts_expr}
        {settings}
        """
        
        print(f"Processing {current_start} -> {current_end} ...")
        try:
            cli.command(query)
        except Exception as e:
            print(f"[Error] Failed on chunk {current_start}: {e}")
            # Optional: raise e if you want to stop on first error
        
        current_start = current_end

    print(json.dumps({
        "status": "ok", 
        "target": args.target, 
        "processed_range": f"{start_dt} to {end_dt}"
    }))

if __name__ == "__main__":
    main()