#!/usr/bin/env python3
"""
Test feature columns health in ClickHouse.

Checks if feature columns are "air columns" (all NULL, all zero, or all constant).
Outputs structured JSON for analysis.

Usage:
    venv/bin/python tools/test_feature_columns_health.py \
        --symbols TSLA,NVDA,SPY \
        --feature-cols ret_1,ret_5,atr_14,spy_ret_5,vix_z_20 \
        --start 2025-11-01 \
        --end   2025-11-24 \
        --table trading.features_1m \
        --out runs/tests/feature_health.json
"""

from __future__ import annotations

import argparse
import json
import math
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
        description="Test feature columns health"
    )
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols"
    )
    ap.add_argument(
        "--feature-cols",
        required=True,
        help="Comma-separated feature column names"
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
        "--table",
        default="trading.features_1m",
        help="ClickHouse table (default: trading.features_1m)"
    )
    ap.add_argument(
        "--out",
        help="Output JSON file path"
    )
    return ap.parse_args()


def adjust_end_date(end: str) -> str:
    """Skip today if end >= today."""
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    today = datetime.now().date()

    if end_dt.date() >= today:
        end_dt = datetime.combine(today - timedelta(days=1), datetime.min.time())

    return end_dt.strftime("%Y-%m-%d")


def check_feature_column(
    client,
    symbol: str,
    feature_col: str,
    start: str,
    end: str,
    table: str
) -> Dict[str, Any]:
    """
    Check health of a single feature column for a symbol.

    Returns:
        {
            "symbol": "TSLA",
            "feature": "ret_5",
            "total_rows": 200000,
            "non_null": 198500,
            "non_null_ratio": 0.9925,
            "all_zero": false,
            "all_constant": false,
            "min": -0.045,
            "max": 0.051,
            "mean": 0.0002,
            "std": 0.008
        }
    """
    result: Dict[str, Any] = {
        "symbol": symbol,
        "feature": feature_col,
    }

    # Convert date strings to datetime objects (ClickHouse client requires this)
    # Add 1 day to end to include the entire end date (ts < end+1)
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)

    # Query to get statistics
    query = f"""
        SELECT
            count() as total_rows,
            countIf({feature_col} IS NOT NULL) as non_null,
            countIf({feature_col} = 0) as zero_count,
            uniq({feature_col}) as distinct_values,
            min({feature_col}) as min_val,
            max({feature_col}) as max_val,
            avg({feature_col}) as mean_val,
            stddevPop({feature_col}) as std_val
        FROM {table}
        WHERE symbol = %(symbol)s
          AND ts >= %(start)s
          AND ts < %(end)s
    """

    try:
        rows = client.query(query, {"symbol": symbol, "start": start_dt, "end": end_dt})
    except Exception as e:
        result["error"] = f"Query failed: {e}"
        return result

    if not rows:
        result["error"] = "No rows returned"
        return result

    row = rows[0]

    total_rows = int(row["total_rows"])
    non_null = int(row["non_null"])
    zero_count = int(row["zero_count"])
    distinct_values = int(row["distinct_values"])

    result["total_rows"] = total_rows
    result["non_null"] = non_null
    result["non_null_ratio"] = round(non_null / total_rows, 4) if total_rows > 0 else 0.0

    # Check if all zero (among non-null values)
    result["all_zero"] = (non_null > 0 and zero_count == non_null)

    # Check if all constant (only 1-2 distinct values, accounting for NULL)
    result["all_constant"] = (distinct_values <= 2 and non_null > 100)

    # Statistics (only meaningful if non_null > 0)
    if non_null > 0:
        min_val = row["min_val"]
        max_val = row["max_val"]
        mean_val = row["mean_val"]
        std_val = row["std_val"]

        # ClickHouse may return NaN for aggregated stats when underlying
        # values are NaN. We treat such cases as "air" even if non_null_ratio
        # is high, because the effective information content is zero.
        def _safe_round(x):
            if x is None:
                return None
            try:
                xf = float(x)
                if math.isnan(xf):
                    return None
                return round(xf, 6)
            except Exception:
                return None

        result["min"] = _safe_round(min_val)
        result["max"] = _safe_round(max_val)
        result["mean"] = _safe_round(mean_val)
        result["std"] = _safe_round(std_val)
    else:
        result["min"] = None
        result["max"] = None
        result["mean"] = None
        result["std"] = None

    return result


def main():
    args = parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    features = [f.strip() for f in args.feature_cols.split(",")]
    end_adjusted = adjust_end_date(args.end)

    print(f"Testing feature columns health:")
    print(f"  Symbols: {symbols}")
    print(f"  Features: {features}")
    print(f"  Period: {args.start} to {end_adjusted}")
    print(f"  Table: {args.table}")
    print()

    client = get_client()
    results: List[Dict[str, Any]] = []

    total_checks = len(symbols) * len(features)
    current = 0

    for symbol in symbols:
        for feature in features:
            current += 1
            result = check_feature_column(
                client,
                symbol,
                feature,
                args.start,
                end_adjusted,
                args.table
            )
            results.append(result)

            # Print progress
            if "error" in result:
                print(f"[{current}/{total_checks}] ❌ {symbol}.{feature}: {result['error']}")
            else:
                cov = result["non_null_ratio"]
                status = "✅" if cov > 0.9 else "⚠️" if cov > 0.5 else "❌"

                issues = []
                if result["all_zero"]:
                    issues.append("ALL_ZERO")
                if result["all_constant"]:
                    issues.append("CONSTANT")

                issue_str = f" [{', '.join(issues)}]" if issues else ""

                print(f"[{current}/{total_checks}] {status} {symbol}.{feature}: "
                      f"coverage={cov:.1%}, "
                      f"range=[{result.get('min', 'N/A')}, {result.get('max', 'N/A')}]"
                      f"{issue_str}")

    # Summary statistics
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)

    total = len(results)
    errors = sum(1 for r in results if "error" in r)
    healthy = sum(
        1 for r in results
        if "error" not in r
        and r["non_null_ratio"] > 0.9
        and not r["all_zero"]
        and not r["all_constant"]
    )
    air_columns = sum(
        1 for r in results
        if "error" not in r
        and (r["non_null_ratio"] < 0.1 or r["all_zero"] or r["all_constant"])
    )

    print(f"Total checks: {total}")
    print(f"Healthy (>90% coverage, not air): {healthy}")
    print(f"Air columns (low coverage or all-zero/constant): {air_columns}")
    print(f"Errors: {errors}")

    # List air columns
    if air_columns > 0:
        print(f"\n⚠️  Air columns detected:")
        for r in results:
            if "error" not in r and (
                r["non_null_ratio"] < 0.1 or r["all_zero"] or r["all_constant"]
            ):
                reason = []
                if r["non_null_ratio"] < 0.1:
                    reason.append(f"coverage={r['non_null_ratio']:.1%}")
                if r["all_zero"]:
                    reason.append("all-zero")
                if r["all_constant"]:
                    reason.append("constant")
                print(f"  - {r['symbol']}.{r['feature']}: {', '.join(reason)}")

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
    if air_columns > 0:
        print(f"\n❌ {air_columns} air columns detected!")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
