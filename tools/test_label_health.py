#!/usr/bin/env python3
"""
Test label columns health in ClickHouse.

Validates label coverage, distribution (pos/neg/zero), and detects
"all-zero" labels that would break ML training.

Usage:
    venv/bin/python tools/test_label_health.py \
        --symbols TSLA,NVDA \
        --label-cols h5_bps10,h10_bps7 \
        --start 2025-11-01 \
        --end   2025-11-24 \
        --table trading.ml_labels_1m \
        --out runs/tests/label_health.json
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
        description="Test label columns health"
    )
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols"
    )
    ap.add_argument(
        "--label-cols",
        required=True,
        help="Comma-separated label column names"
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
        default="trading.ml_labels_1m",
        help="ClickHouse table (default: trading.ml_labels_1m)"
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


def check_label_column(
    client,
    symbol: str,
    label_col: str,
    start: str,
    end: str,
    table: str
) -> Dict[str, Any]:
    """
    Check health of a single label column for a symbol.

    Returns:
        {
            "symbol": "TSLA",
            "label": "h5_bps10",
            "total_rows": 180000,
            "non_null": 179000,
            "non_zero": 12000,
            "non_zero_ratio": 0.067,
            "pos": 6200,
            "neg": 5800,
            "zero": 167000
        }
    """
    result: Dict[str, Any] = {
        "symbol": symbol,
        "label": label_col,
    }

    # Convert date strings to datetime objects (ClickHouse client requires this)
    # Add 1 day to end to include the entire end date (ts < end+1)
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)

    # Query to get label statistics
    query = f"""
        SELECT
            count() as total_rows,
            countIf({label_col} IS NOT NULL) as non_null,
            countIf({label_col} != 0) as non_zero,
            countIf({label_col} > 0) as pos_count,
            countIf({label_col} < 0) as neg_count,
            countIf({label_col} = 0) as zero_count
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
    non_zero = int(row["non_zero"])
    pos_count = int(row["pos_count"])
    neg_count = int(row["neg_count"])
    zero_count = int(row["zero_count"])

    result["total_rows"] = total_rows
    result["non_null"] = non_null
    result["non_zero"] = non_zero
    result["non_zero_ratio"] = round(non_zero / total_rows, 4) if total_rows > 0 else 0.0
    result["pos"] = pos_count
    result["neg"] = neg_count
    result["zero"] = zero_count

    # Calculate imbalance ratio (useful for ML)
    if non_zero > 0:
        result["pos_ratio"] = round(pos_count / non_zero, 4)
        result["neg_ratio"] = round(neg_count / non_zero, 4)
    else:
        result["pos_ratio"] = 0.0
        result["neg_ratio"] = 0.0

    return result


def main():
    args = parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    labels = [l.strip() for l in args.label_cols.split(",")]
    end_adjusted = adjust_end_date(args.end)

    print(f"Testing label columns health:")
    print(f"  Symbols: {symbols}")
    print(f"  Labels: {labels}")
    print(f"  Period: {args.start} to {end_adjusted}")
    print(f"  Table: {args.table}")
    print()

    client = get_client()
    results: List[Dict[str, Any]] = []

    total_checks = len(symbols) * len(labels)
    current = 0

    for symbol in symbols:
        for label in labels:
            current += 1
            result = check_label_column(
                client,
                symbol,
                label,
                args.start,
                end_adjusted,
                args.table
            )
            results.append(result)

            # Print progress
            if "error" in result:
                print(f"[{current}/{total_checks}] ❌ {symbol}.{label}: {result['error']}")
            else:
                non_zero_ratio = result["non_zero_ratio"]
                status = "✅" if non_zero_ratio > 0.01 else "⚠️" if non_zero_ratio > 0 else "❌"

                issues = []
                if non_zero_ratio == 0:
                    issues.append("ALL_ZERO")
                elif non_zero_ratio < 0.001:
                    issues.append("VERY_SPARSE")

                # Check for extreme imbalance
                if result["non_zero"] > 0:
                    if result["pos_ratio"] > 0.99 or result["pos_ratio"] < 0.01:
                        issues.append("IMBALANCED")

                issue_str = f" [{', '.join(issues)}]" if issues else ""

                print(f"[{current}/{total_checks}] {status} {symbol}.{label}: "
                      f"non_zero={non_zero_ratio:.1%}, "
                      f"pos={result['pos']}, neg={result['neg']}, zero={result['zero']}"
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
        and r["non_zero_ratio"] > 0.01
        and 0.05 < r.get("pos_ratio", 0.5) < 0.95
    )
    all_zero = sum(
        1 for r in results
        if "error" not in r and r["non_zero_ratio"] == 0
    )
    very_sparse = sum(
        1 for r in results
        if "error" not in r and 0 < r["non_zero_ratio"] < 0.001
    )

    print(f"Total checks: {total}")
    print(f"Healthy (>1% non-zero, balanced): {healthy}")
    print(f"All-zero labels: {all_zero}")
    print(f"Very sparse (<0.1% non-zero): {very_sparse}")
    print(f"Errors: {errors}")

    # List problematic labels
    if all_zero + very_sparse > 0:
        print(f"\n⚠️  Problematic labels:")
        for r in results:
            if "error" not in r and (r["non_zero_ratio"] == 0 or r["non_zero_ratio"] < 0.001):
                reason = "all-zero" if r["non_zero_ratio"] == 0 else f"very sparse ({r['non_zero_ratio']:.3%})"
                print(f"  - {r['symbol']}.{r['label']}: {reason}")

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
    if all_zero > 0:
        print(f"\n❌ {all_zero} all-zero labels detected!")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
