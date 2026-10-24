#!/usr/bin/env python3
"""
Test active table coverage consistency.

Compares coverage between:
- bars_1m_ibkr (raw bars)
- features_1m (baseline features)
- features_1m_active (active/gateway features)

Detects cases where features_1m has good coverage but active table is underfilled.

Usage:
    venv/bin/python tools/test_active_coverage_consistency.py \
        --symbols TSLA,SPY \
        --date 2025-11-24 \
        --min-bars 300 \
        --out runs/tests/active_coverage.json
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
        description="Test active table coverage consistency"
    )
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols"
    )
    ap.add_argument(
        "--date",
        required=True,
        help="Date to check (YYYY-MM-DD)"
    )
    ap.add_argument(
        "--min-bars",
        type=int,
        default=300,
        help="Minimum expected bars (default: 300, ~77%% of RTH)"
    )
    ap.add_argument(
        "--out",
        help="Output JSON file path"
    )
    return ap.parse_args()


def adjust_date(date_str: str) -> str:
    """
    Skip today if date >= today.

    Returns the adjusted date or None if date is in the future.
    """
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    today = datetime.now().date()

    if date_dt.date() >= today:
        print(f"⚠️  Date {date_str} is today or in the future, skipping", file=sys.stderr)
        return None

    return date_str


def check_active_consistency(
    client,
    symbol: str,
    date: str,
    min_bars: int
) -> Dict[str, Any]:
    """
    Check active table consistency for a single symbol on a date.

    Returns:
        {
            "symbol": "SPY",
            "date": "2025-11-24",
            "bars_count": 390,
            "features_rows": 388,
            "active_rows": 90,
            "features_cov": 0.994,
            "active_cov": 0.231,
            "active_underfilled": true
        }
    """
    result: Dict[str, Any] = {
        "symbol": symbol,
        "date": date,
    }

    # Query 1: Count bars from bars_1m_ibkr
    bars_query = """
        SELECT count() as cnt
        FROM trading.bars_1m_ibkr
        WHERE symbol = %(symbol)s
          AND toDate(ts_end) = %(date)s
          AND toHour(ts_end) >= 9
          AND toHour(ts_end) < 16
    """

    # Query 2: Count rows from features_1m
    features_query = """
        SELECT count() as cnt
        FROM trading.features_1m
        WHERE symbol = %(symbol)s
          AND toDate(ts) = %(date)s
          AND toHour(ts) >= 9
          AND toHour(ts) < 16
    """

    # Query 3: Count rows from features_1m_active
    active_query = """
        SELECT count() as cnt
        FROM trading.features_1m_active
        WHERE symbol = %(symbol)s
          AND toDate(ts) = %(date)s
          AND toHour(ts) >= 9
          AND toHour(ts) < 16
    """

    params = {"symbol": symbol, "date": date}

    try:
        bars_rows = client.query(bars_query, params)
        features_rows = client.query(features_query, params)
        active_rows = client.query(active_query, params)
    except Exception as e:
        result["error"] = f"Query failed: {e}"
        return result

    bars_count = int(bars_rows[0]["cnt"]) if bars_rows else 0
    features_count = int(features_rows[0]["cnt"]) if features_rows else 0
    active_count = int(active_rows[0]["cnt"]) if active_rows else 0

    result["bars_count"] = bars_count
    result["features_rows"] = features_count
    result["active_rows"] = active_count

    # Calculate coverage ratios
    if bars_count > 0:
        result["features_cov"] = round(features_count / bars_count, 4)
        result["active_cov"] = round(active_count / bars_count, 4)
    else:
        result["features_cov"] = 0.0
        result["active_cov"] = 0.0

    # Detect active underfilling:
    # - Features have good coverage (>0.9)
    # - But active has poor coverage (<0.5)
    result["active_underfilled"] = (
        result["features_cov"] > 0.9 and
        result["active_cov"] < 0.5
    )

    # Detect insufficient data overall
    result["insufficient_bars"] = (bars_count < min_bars)

    return result


def main():
    args = parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    date = adjust_date(args.date)

    if date is None:
        return 1

    print(f"Testing active coverage consistency:")
    print(f"  Symbols: {symbols}")
    print(f"  Date: {date}")
    print(f"  Min bars threshold: {args.min_bars}")
    print()

    client = get_client()
    results: List[Dict[str, Any]] = []

    for symbol in symbols:
        result = check_active_consistency(
            client,
            symbol,
            date,
            args.min_bars
        )
        results.append(result)

        # Print progress
        if "error" in result:
            print(f"❌ {symbol}: {result['error']}")
        else:
            bars = result["bars_count"]
            features_cov = result["features_cov"]
            active_cov = result["active_cov"]

            # Status indicator
            if result["insufficient_bars"]:
                status = "⚠️"
                msg = f"insufficient bars ({bars} < {args.min_bars})"
            elif result["active_underfilled"]:
                status = "❌"
                msg = f"active underfilled (features={features_cov:.1%}, active={active_cov:.1%})"
            elif active_cov < 0.9:
                status = "⚠️"
                msg = f"active low (features={features_cov:.1%}, active={active_cov:.1%})"
            else:
                status = "✅"
                msg = f"healthy (features={features_cov:.1%}, active={active_cov:.1%})"

            print(f"{status} {symbol}: bars={bars}, {msg}")

    # Summary statistics
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)

    total = len(results)
    errors = sum(1 for r in results if "error" in r)
    underfilled = sum(1 for r in results if "error" not in r and r.get("active_underfilled", False))
    insufficient = sum(1 for r in results if "error" not in r and r.get("insufficient_bars", False))
    healthy = sum(
        1 for r in results
        if "error" not in r
        and not r.get("active_underfilled", False)
        and not r.get("insufficient_bars", False)
        and r.get("active_cov", 0) > 0.9
    )

    print(f"Total checks: {total}")
    print(f"Healthy (active >90%): {healthy}")
    print(f"Active underfilled: {underfilled}")
    print(f"Insufficient bars: {insufficient}")
    print(f"Errors: {errors}")

    # List underfilled symbols
    if underfilled > 0:
        print(f"\n⚠️  Active underfilled symbols (features good, active poor):")
        for r in results:
            if "error" not in r and r.get("active_underfilled", False):
                print(f"  - {r['symbol']}: features={r['features_cov']:.1%}, active={r['active_cov']:.1%}")
                print(f"    → Need to run auto-seed or check_active_completeness")

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
    if underfilled > 0:
        print(f"\n❌ {underfilled} symbols have active underfilling issues!")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
