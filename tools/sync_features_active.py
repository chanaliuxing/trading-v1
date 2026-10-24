#!/usr/bin/env python3
from __future__ import annotations

"""
Incremental sync from baseline features table to active table with schema alignment.
Intended as a safe utility the Meta-Agent can call before training.
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List

# Ensure project root on path so libs imports work when run from tools/
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from libs.db.ch_pool import get_client
except Exception:
    get_client = None  # type: ignore


def _align_schema(ch, source: str, target: str) -> None:
    src_cols = ch.query(f"DESCRIBE TABLE {source}")
    tgt_cols = ch.query(f"DESCRIBE TABLE {target}")
    src_names = [r["name"] for r in src_cols]
    tgt_names = [r["name"] for r in tgt_cols]
    missing = [c for c in src_names if c not in tgt_names]
    for col in src_cols:
        if col["name"] in missing:
            ch.command(
                f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS {col['name']} {col['type']} AFTER {src_names[max(src_names.index(col['name'])-1,0)]}"
            )


def _common_columns(ch, source: str, target: str) -> List[str]:
    """Determine column list shared by source and target, preserving target order."""
    src_cols = ch.query(f"DESCRIBE TABLE {source}")
    tgt_cols = ch.query(f"DESCRIBE TABLE {target}")
    src_names = {row["name"] for row in src_cols}
    return [row["name"] for row in tgt_cols if row["name"] in src_names]


def _rows_written(result) -> int:
    """Extract written row count from clickhouse_connect responses."""
    if isinstance(result, (int, float)):
        return int(result)
    if isinstance(result, dict):
        return int(result.get("written_rows") or result.get("rowcount") or 0)
    summary = getattr(result, "summary", None)
    if isinstance(summary, dict):
        return int(summary.get("written_rows") or summary.get("rowcount") or 0)
    return 0


def sync_range(
    ch,
    source: str,
    target: str,
    symbols: List[str],
    start: datetime,
    end: datetime,
    chunk_days: int,
    columns: List[str],
) -> int:
    total = 0
    cur = start
    if not columns:
        raise RuntimeError("No common columns between source and target tables")
    cols_sql = ", ".join(f"`{col}`" for col in columns)
    while cur < end:
        nxt = min(end, cur + timedelta(days=chunk_days))
        rows = ch.command(
            f"""
            INSERT INTO {target} ({cols_sql})
            SELECT {cols_sql} FROM {source}
            WHERE symbol IN %(syms)s
              AND ts >= toDateTime(%(start)s)
              AND ts < toDateTime(%(end)s)
            """,
            parameters={"syms": symbols, "start": cur, "end": nxt},
            settings={"max_memory_usage": 8_000_000_000},
        )
        total += _rows_written(rows)
        cur = nxt
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", required=True, help="Comma separated symbols")
    parser.add_argument("--start", required=True, help="Start datetime, e.g. 2025-11-01 09:30:00")
    parser.add_argument("--end", required=True, help="End datetime")
    parser.add_argument("--source", default="trading.features_1m")
    parser.add_argument("--target", default="trading.features_1m_active")
    parser.add_argument("--chunk-days", type=int, default=1)
    args = parser.parse_args()

    if not get_client:
        print("No ClickHouse client available")
        return
    ch = get_client()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start_dt = datetime.fromisoformat(args.start)
    end_dt = datetime.fromisoformat(args.end)

    _align_schema(ch, args.source, args.target)
    columns = _common_columns(ch, args.source, args.target)
    inserted = sync_range(ch, args.source, args.target, syms, start_dt, end_dt, args.chunk_days, columns)
    print(f"synced_rows={inserted}")


if __name__ == "__main__":
    main()
