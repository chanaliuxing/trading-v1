#!/usr/bin/env python3
"""
Test raw 1m bars health from ClickHouse.

Validates that bars_1m_ibkr has proper coverage for each symbol/day,
checks for gaps, and outputs structured JSON for analysis.

Usage:
    venv/bin/python tools/test_raw_bars_health.py \
        --symbols TSLA,SPY,NVDA \
        --start 2025-11-01 \
        --end   2025-11-24 \
        --bar-size 1m \
        --out runs/tests/raw_bars_health.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from libs.db.ch_pool import get_client


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Test raw bars health from ClickHouse"
    )
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols (e.g., TSLA,SPY,NVDA)"
    )
    ap.add_argument(
        "--start",
        required=True,
        help="Start date (YYYY-MM-DD)"
    )
    ap.add_argument(
        "--end",
        required=True,
        help="End date (YYYY-MM-DD), will auto-skip if today"
    )
    ap.add_argument(
        "--bar-size",
        default="1m",
        help="Bar size (default: 1m)"
    )
    ap.add_argument(
        "--table",
        default="trading.bars_1m_ibkr",
        help="ClickHouse table (default: trading.bars_1m_ibkr)"
    )
    ap.add_argument(
        "--max-gap-threshold",
        type=int,
        default=5,
        help="Max gap threshold in minutes (default: 5)"
    )
    ap.add_argument(
        "--out",
        help="Output JSON file path"
    )
    return ap.parse_args()


def get_market_days(start: str, end: str) -> List[str]:
    """
    Get list of market days (Mon-Fri) in the date range.

    Automatically excludes today if end >= today.
    """
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")

    # Skip today (incomplete data)
    today = datetime.now().date()
    if end_dt.date() >= today:
        end_dt = datetime.combine(today - timedelta(days=1), datetime.min.time())

    days = []
    current = start_dt
    while current <= end_dt:
        # Mon-Fri only (0-4)
        if current.weekday() < 5:
            days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return days


def check_bars_for_day(
    client,
    symbol: str,
    day: str,
    table: str,
    max_gap_threshold: int
) -> Dict[str, Any]:
    """
    Check bars health for a single symbol on a single day.

    Returns:
        {
            "symbol": "TSLA",
            "day": "2025-11-22",
            "expected_bars": 390,
            "actual_bars": 388,
            "coverage": 0.994,
            "max_gap_min": 3
        }
    """
    # RTH: 09:30-16:00 = 6.5 hours = 390 minutes
    expected_bars = 390

    # Query to get bar count and timestamps
    query = f"""
        SELECT
            toDateTime(ts_end) as ts
        FROM {table}
        WHERE symbol = %(symbol)s
          AND toDate(ts_end) = %(day)s
          AND toHour(ts_end) >= 9
          AND toHour(ts_end) < 16
        ORDER BY ts_end
    """

    try:
        rows = client.query(query, {"symbol": symbol, "day": day})
    except Exception as e:
        return {
            "symbol": symbol,
            "day": day,
            "error": f"Query failed: {e}",
            "expected_bars": expected_bars,
            "actual_bars": 0,
            "coverage": 0.0,
            "max_gap_min": None
        }

    actual_bars = len(rows)
    coverage = actual_bars / expected_bars if expected_bars > 0 else 0.0

    # Calculate max gap
    max_gap_min = 0
    if len(rows) > 1:
        timestamps = [r["ts"] for r in rows]
        for i in range(1, len(timestamps)):
            gap = (timestamps[i] - timestamps[i-1]).total_seconds() / 60.0
            # Normal gap is 1 minute, anything > threshold is concerning
            if gap > max_gap_threshold:
                max_gap_min = max(max_gap_min, int(gap))

    return {
        "symbol": symbol,
        "day": day,
        "expected_bars": expected_bars,
        "actual_bars": actual_bars,
        "coverage": round(coverage, 4),
        "max_gap_min": max_gap_min if max_gap_min > 0 else 0
    }


def main():
    args = parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    market_days = get_market_days(args.start, args.end)

    if not market_days:
        print("No market days in range (or today was excluded)", file=sys.stderr)
        return 1

    print(f"Testing raw bars health:")
    print(f"  Symbols: {symbols}")
    print(f"  Market days: {len(market_days)} ({market_days[0]} to {market_days[-1]})")
    print(f"  Table: {args.table}")
    print()

    client = get_client()
    results: List[Dict[str, Any]] = []

    total_checks = len(symbols) * len(market_days)
    current = 0

    for symbol in symbols:
        for day in market_days:
            current += 1
            result = check_bars_for_day(
                client,
                symbol,
                day,
                args.table,
                args.max_gap_threshold
            )
            results.append(result)

            # Print progress
            status = "✅" if result["coverage"] > 0.95 else "⚠️" if result["coverage"] > 0.7 else "❌"
            print(f"[{current}/{total_checks}] {status} {symbol} {day}: "
                  f"{result['actual_bars']}/{result['expected_bars']} bars "
                  f"({result['coverage']:.1%}), max_gap={result['max_gap_min']}min")

    # Summary statistics
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)

    total = len(results)
    high_cov = sum(1 for r in results if r["coverage"] > 0.95)
    medium_cov = sum(1 for r in results if 0.7 < r["coverage"] <= 0.95)
    low_cov = sum(1 for r in results if r["coverage"] <= 0.7)

    print(f"Total checks: {total}")
    print(f"High coverage (>95%): {high_cov}")
    print(f"Medium coverage (70-95%): {medium_cov}")
    print(f"Low coverage (≤70%): {low_cov}")

    # Large gaps (handle None values)
    large_gaps = [
        r for r in results
        if r.get("max_gap_min") is not None
        and r.get("max_gap_min", 0) > args.max_gap_threshold
    ]
    if large_gaps:
        print(f"\n⚠️  Found {len(large_gaps)} symbol-days with gaps > {args.max_gap_threshold}min:")
        for r in large_gaps[:10]:  # Show first 10
            print(f"  - {r['symbol']} {r['day']}: {r['max_gap_min']}min gap")

    # Output JSON
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\n✅ Results saved to: {out_path}")
    else:
        print("\n" + json.dumps(results, indent=2))

    # Exit code based on quality
    if low_cov > 0:
        print(f"\n❌ {low_cov} symbol-days have low coverage (≤70%)")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
