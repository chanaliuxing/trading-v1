#!/usr/bin/env python3
"""
Check active table (trading.features_1m_active) completeness vs bars_1m_ibkr for a date window.

Outputs JSON summary to stdout and exits with non-zero if coverage < threshold for any symbol.

Usage:
  python tools/check_active_completeness.py --date 2025-11-05 --symbols TSLA,AAPL --min-coverage 0.98
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import List, Dict, Any, Tuple

import clickhouse_connect


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="Trading date YYYY-MM-DD")
    ap.add_argument("--symbols", default="", help="Comma-separated symbols; empty=use all from active table for date")
    ap.add_argument("--min-coverage", type=float, default=0.98, help="Minimum coverage threshold (0..1)")
    ap.add_argument("--start", default="09:30:00", help="Session start HH:MM:SS (ignored if --full-day)")
    ap.add_argument("--end", default="16:00:00", help="Session end HH:MM:SS (ignored if --full-day)")
    ap.add_argument("--full-day", action="store_true", help="Use full-day window (00:00:00-23:59:59) instead of RTH")
    ap.add_argument("--bars-table", default="trading.bars_1m_ibkr")
    ap.add_argument("--active-table", default="trading.features_1m_active")
    ap.add_argument("--baseline-table", default="trading.features_1m", help="Baseline features table used to seed active when empty")
    ap.add_argument("--auto-seed-active", action="store_true", help="When active coverage is 0 and bars>0, seed from baseline table for the window")
    return ap.parse_args()


def _connect() -> Any:
    try:
        from libs.config import config
        ccfg = config.clickhouse_config
        return clickhouse_connect.get_client(
            host=ccfg.get("host", "localhost"),
            port=int(ccfg.get("port", 8123)),
            database=ccfg.get("database", "trading"),
            username=ccfg.get("user", "default"),
            password=ccfg.get("password", "")
        )
    except Exception:
        return clickhouse_connect.get_client(host="localhost", port=8123, database="trading")


def _describe_columns(cli, table: str) -> List[str]:
    try:
        rows = cli.query(f"DESCRIBE TABLE {table}").result_rows
        return [r[0] for r in rows]
    except Exception:
        return []


def _describe_columns_full(cli, table: str) -> List[Dict[str, Any]]:
    """Return list of column metadata dicts with keys: name,type,default_expression (best-effort)."""
    try:
        rows = cli.query(f"DESCRIBE TABLE {table}").named_results()
        out = []
        for r in rows:
            out.append({
                "name": r.get("name") or r.get("column", ""),
                "type": r.get("type", ""),
                "default_expression": r.get("default_expression") or r.get("default_expr") or "",
            })
        return out
    except Exception:
        return []


def _resolve_ts_column(cli, table: str) -> str:
    """Return the timestamp column name for table among common candidates.

    Falls back to 'ts' if detection fails.
    """
    cols = set(_describe_columns(cli, table))
    # Priority order: prefer ts_end for bar data, then other common names
    for cand in ("ts_end", "ts_start", "timestamp", "bar_time", "ts", "bar_ts", "time", "dt"):
        if cand in cols:
            return cand
    return "ts"  # fallback (will likely fail if reached)


def _list_syms(cli, active_table: str, date_str: str) -> List[str]:
    ts_col = _resolve_ts_column(cli, active_table)
    q = f"""
    SELECT DISTINCT symbol
    FROM {active_table}
    WHERE {ts_col} >= toDateTime(%(s)s) AND {ts_col} < toDateTime(%(e)s)
    """
    s = f"{date_str} 00:00:00"
    e = f"{date_str} 23:59:59"
    rows = cli.query(q, parameters={"s": s, "e": e}).result_rows
    return [r[0] for r in rows]


def _count(cli, table: str, sym: str, s: str, e: str) -> int:
    ts_col = _resolve_ts_column(cli, table)
    q = f"""
    SELECT count() FROM {table}
    WHERE symbol = %(sym)s AND {ts_col} >= toDateTime(%(s)s) AND {ts_col} < toDateTime(%(e)s)
    """
    return cli.query(q, parameters={"sym": sym, "s": s, "e": e}).result_rows[0][0]


def _ensure_active_table(cli, active_table: str, baseline_table: str) -> None:
    try:
        cli.query(f"SELECT 1 FROM {active_table} LIMIT 1")
    except Exception:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {active_table}
        AS {baseline_table}
        ENGINE = ReplacingMergeTree()
        ORDER BY (symbol, ts)
        TTL ts + INTERVAL 7 DAY
        """
        cli.command(ddl)


def _seed_active(cli, baseline_table: str, active_table: str, sym: str, s: str, e: str) -> int:
    """Insert rows from baseline into active with column mapping and ts aliasing.

    Returns number of newly visible rows in active for (sym, window).
    """
    try:
        # First, delete any existing data for this window to ensure a clean insert
        # This is critical for ReplacingMergeTree tables to behave predictably
        delete_query = f"""
        ALTER TABLE {active_table} DELETE WHERE symbol = %(sym)s AND toDate(ts) = toDate(toDateTime(%(s)s))
        """
        cli.command(delete_query, parameters={"sym": sym, "s": s}, settings={'mutations_sync': 2})
        
        before = 0 # Known to be 0 after delete

        # Build column mapping
        src_cols = [c.get("name") for c in _describe_columns_full(cli, baseline_table) if c.get("name")]
        dst_cols = [c.get("name") for c in _describe_columns_full(cli, active_table) if c.get("name")]
        if not src_cols or not dst_cols:
            return 0

        # Timestamp alias if names differ
        src_ts = _resolve_ts_column(cli, baseline_table)
        dst_ts = _resolve_ts_column(cli, active_table)

        # Common columns (preserve destination order)
        common = [c for c in dst_cols if c in src_cols]

        # Ensure timestamp present via alias if needed
        select_exprs: List[str] = []
        insert_cols: List[str] = []
        for col in dst_cols:
            if col == dst_ts and src_ts != dst_ts and src_ts in src_cols:
                insert_cols.append(dst_ts)
                select_exprs.append(f"{src_ts} AS {dst_ts}")
            elif col in common:
                insert_cols.append(col)
                select_exprs.append(col)
            else:
                # Skip non-common, rely on defaults in active table
                continue

        if not insert_cols:
            return 0

        # Check how many rows we expect to select
        count_query = f"SELECT count() FROM {baseline_table} WHERE symbol = %(sym)s AND {src_ts} >= toDateTime(%(s)s) AND {src_ts} < toDateTime(%(e)s)"
        source_count = cli.query(count_query, parameters={"sym": sym, "s": s, "e": e}).result_rows[0][0]
        print(f"[dq_debug] Rows to be selected from '{baseline_table}': {source_count}")


        ins = f"""
        INSERT INTO {active_table} ({', '.join(insert_cols)})
        SELECT {', '.join(select_exprs)} FROM {baseline_table}
        WHERE symbol = %(sym)s AND {src_ts} >= toDateTime(%(s)s) AND {src_ts} < toDateTime(%(e)s)
        """
        cli.command(ins, parameters={"sym": sym, "s": s, "e": e})
        
        after_insert = _count(cli, active_table, sym, s, e)
        print(f"[dq_debug] Row count in '{active_table}' after INSERT: {after_insert}")

        cli.command(f"OPTIMIZE TABLE {active_table} FINAL") # Force merge to apply ReplacingMergeTree logic
        print(f"[dq_debug] OPTIMIZE TABLE FINAL executed on '{active_table}'")

        after_optimize = _count(cli, active_table, sym, s, e)
        print(f"[dq_debug] Row count in '{active_table}' after OPTIMIZE: {after_optimize}")

        return max(0, after_optimize - before)
    except Exception as ex:
        print(f"[dq] Auto-seed failed for {sym}: {ex}")
        return 0


def main() -> None:
    args = _parse_args()
    date_str = args.date
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("Invalid --date format; expected YYYY-MM-DD")

    if args.full_day:
        s = f"{date_str} 00:00:00"
        e = f"{date_str} 23:59:59"
    else:
        s = f"{date_str} {args.start}"
        e = f"{date_str} {args.end}"

    cli = _connect()
    syms: List[str]
    if args.symbols.strip():
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = _list_syms(cli, args.active_table, date_str)

    results: Dict[str, Any] = {
        "date": date_str,
        "start": s,
        "end": e,
        "min_coverage": args.min_coverage,
        "symbols": [],
    }
    bad = False
    for sym in syms:
        n_bars = _count(cli, args.bars_table, sym, s, e)
        n_active = _count(cli, args.active_table, sym, s, e)
        # Optional auto-seed when coverage is clearly insufficient but bars exist.
        # If n_active is zero OR coverage below threshold, attempt a best-effort
        # repair by seeding from the baseline features table.
        if args.auto_seed_active and n_bars > 0:
            cov_before = (n_active / n_bars) if n_bars > 0 else 0.0
            if n_active == 0 or cov_before < args.min_coverage:
                _ensure_active_table(cli, args.active_table, args.baseline_table)
                inserted = _seed_active(cli, args.baseline_table, args.active_table, sym, s, e)
                if inserted > 0:
                    n_active = _count(cli, args.active_table, sym, s, e)
        # Special case: no bars at all in the window (market closed or IBKR not providing data yet).
        # In this case we do NOT treat coverage as a hard failure for ML/data gates; mark coverage=1.0
        # and attach a note so higher layers can distinguish this from true data gaps.
        if n_bars == 0:
            cov = 1.0
            results["symbols"].append(
                {
                    "symbol": sym,
                    "bars": n_bars,
                    "active": n_active,
                    "coverage": round(cov, 4),
                    "note": "no_bars_in_window",
                }
            )
            # Do not mark bad in this case: we skip strict intraday coverage checks when there is
            # literally no underlying bar data (e.g., before session end or IBKR not ready).
        else:
            raw_cov = (n_active / n_bars) if n_bars > 0 else 0.0
            # Clamp to [0,1] to avoid pathological values when seeding inserts
            # duplicate rows in a ReplacingMergeTree that has not merged yet.
            cov = min(1.0, max(0.0, raw_cov))
            results["symbols"].append(
                {"symbol": sym, "bars": n_bars, "active": n_active, "coverage": round(cov, 4)}
            )
            if cov < args.min_coverage:
                bad = True

    print(json.dumps(results, indent=2))
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
