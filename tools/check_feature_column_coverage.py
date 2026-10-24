#!/usr/bin/env python3
"""
Check non-null coverage of feature columns for specific symbols.

Usage:
  venv/bin/python tools/check_feature_column_coverage.py \
    --symbols TSLA,NVDA \
    --days 60

This is intended to:
  - Identify columns that are structurally NULL for a given symbol
    over a recent window (e.g. spy_ret_5 for TSLA/NVDA).
  - Provide evidence to tighten DSL / Prompt so the AI Brain avoids
    columns that are unusable for the current training universe.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

import sys
from pathlib import Path

# Ensure project root is on sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from libs.db.ch_pool import get_client


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols to inspect (e.g. TSLA,NVDA)",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=60,
        help="Lookback window in days (default: 60)",
    )
    ap.add_argument(
        "--table",
        default="trading.features_1m",
        help="ClickHouse table to inspect (default: trading.features_1m)",
    )
    return ap.parse_args()


def _list_numeric_columns(cli, table: str) -> List[str]:
    """Return a list of numeric columns to inspect (skip symbol/ts/meta)."""
    rows = cli.query(f"DESCRIBE TABLE {table}")
    cols: List[str] = []
    for r in rows:
        name = r.get("name") or r.get("column")
        ctype = (r.get("type") or "").lower()
        if not name:
            continue
        if name in {"symbol", "ts", "created_at", "ver"}:
            continue
        # Heuristic: focus on numeric types
        if any(t in ctype for t in ("int", "float", "decimal")):
            cols.append(name)
    return cols


def main() -> None:
    args = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    days = int(args.days)
    table = args.table

    cli = get_client()
    cols = _list_numeric_columns(cli, table)

    results: List[Dict[str, Any]] = []

    for sym in symbols:
        for col in cols:
            q = f"""
            SELECT
              count() AS total,
              countIf({col} IS NOT NULL) AS non_null
            FROM {table}
            WHERE symbol = %(sym)s
              AND ts >= now() - INTERVAL %(days)s DAY
            """
            try:
                res = cli.query(
                    q,
                    parameters={"sym": sym, "days": days},
                )
                row = res[0] if res else {"total": 0, "non_null": 0}
                total = int(row.get("total", 0) or 0)
                non_null = int(row.get("non_null", 0) or 0)
                ratio = float(non_null) / float(total) if total > 0 else 0.0
                results.append(
                    {
                        "symbol": sym,
                        "column": col,
                        "total": total,
                        "non_null": non_null,
                        "non_null_ratio": round(ratio, 4),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "symbol": sym,
                        "column": col,
                        "error": str(exc),
                    }
                )

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
