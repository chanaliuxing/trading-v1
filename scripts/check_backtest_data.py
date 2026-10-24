#!/usr/bin/env python3
"""
Check Data Availability for RevE Strategy Backtest

This script verifies that all required data is available in ClickHouse
before running a backtest.

Usage:
    python scripts/check_backtest_data.py --symbols TSLA NVDA --days 90
"""
import argparse
import sys
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.clickhouse import ClickHouseDB


def check_data_availability(symbols, start_date, end_date):
    """Check if bars and features data exists for the given symbols and date range."""

    print("=" * 80)
    print("Checking Data Availability for Backtest")
    print("=" * 80)
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Date Range: {start_date} to {end_date}")
    print("=" * 80)
    print()

    # Connect to ClickHouse
    try:
        ch = ClickHouseDB()
    except Exception as e:
        print(f"❌ Failed to connect to ClickHouse: {e}")
        return False

    # Tables to check
    tables = [
        ("trading.bars_1m_ibkr", "1-minute bars"),
        ("trading.bars_5m_ibkr", "5-minute bars"),
        ("trading.features_1m", "1-minute features"),
        ("trading.features_5m", "5-minute features"),
    ]

    all_good = True

    for table, description in tables:
        print(f"Checking {description} ({table}):")
        print("-" * 80)

        # Determine the timestamp column name
        ts_col = "ts_start" if "bars_" in table else "ts"

        for symbol in symbols:
            # Count rows for this symbol in date range
            query = f"""
            SELECT
                count() as row_count,
                min({ts_col}) as earliest,
                max({ts_col}) as latest
            FROM {table} FINAL
            WHERE symbol = '{symbol}'
              AND {ts_col} >= '{start_date} 00:00:00'
              AND {ts_col} <= '{end_date} 23:59:59'
            """

            try:
                rows = ch.query(query)
                if rows:
                    row = rows[0]
                    row_count = row.get("row_count", 0)
                    earliest = row.get("earliest")
                    latest = row.get("latest")

                    if row_count == 0:
                        print(f"  ❌ {symbol:<8} NO DATA")
                        all_good = False
                    else:
                        # Calculate expected rows
                        if "1m" in table:
                            # 390 minutes per trading day (9:30-16:00)
                            days = (datetime.strptime(end_date, "%Y-%m-%d") -
                                   datetime.strptime(start_date, "%Y-%m-%d")).days + 1
                            expected_rows = days * 390  # Rough estimate
                        elif "5m" in table:
                            days = (datetime.strptime(end_date, "%Y-%m-%d") -
                                   datetime.strptime(start_date, "%Y-%m-%d")).days + 1
                            expected_rows = days * 78  # 390/5 = 78 bars per day
                        else:
                            expected_rows = 0

                        coverage_pct = (row_count / expected_rows * 100) if expected_rows > 0 else 0

                        status = "✓" if coverage_pct > 80 else "⚠️"
                        print(f"  {status} {symbol:<8} {row_count:>6,} rows "
                              f"({coverage_pct:.1f}% coverage) "
                              f"[{earliest} to {latest}]")

                        if coverage_pct < 80:
                            all_good = False

                else:
                    print(f"  ❌ {symbol:<8} TABLE EMPTY OR MISSING")
                    all_good = False

            except Exception as e:
                print(f"  ❌ {symbol:<8} ERROR: {e}")
                all_good = False

        print()

    # Check for required feature columns
    print("Checking Feature Columns:")
    print("-" * 80)

    required_1m_features = ["atr_14", "ema_20", "vwap", "close", "volume"]
    required_5m_features = ["atr_14", "close"]

    for table, features in [
        ("trading.features_1m", required_1m_features),
        ("trading.features_5m", required_5m_features),
    ]:
        try:
            # Get one row to check columns (use specific symbol and recent date to avoid memory issues)
            query = f"SELECT * FROM {table} FINAL WHERE symbol = '{symbols[0]}' ORDER BY ts DESC LIMIT 1"
            rows = ch.query(query)

            if rows:
                row = rows[0]
                available_cols = set(row.keys())
                missing_cols = set(features) - available_cols

                if missing_cols:
                    print(f"  ❌ {table}: Missing columns: {', '.join(missing_cols)}")
                    all_good = False
                else:
                    print(f"  ✓ {table}: All required columns present")
            else:
                print(f"  ⚠️ {table}: No data to verify columns")

        except Exception as e:
            print(f"  ❌ {table}: Error checking columns: {e}")
            all_good = False

    print()
    print("=" * 80)

    if all_good:
        print("✓ All required data is available!")
        print("=" * 80)
        print()
        print("You can now run the backtest:")
        print(f"  python run_backtest_reve.py --symbols {' '.join(symbols)} --start {start_date} --end {end_date}")
        print()
        return True
    else:
        print("❌ Some data is missing or incomplete!")
        print("=" * 80)
        print()
        print("To backfill missing data:")
        for symbol in symbols:
            print(f"  # Backfill 1-minute and 5-minute bars for {symbol}")
            print(f"  python tool/load_hist_multi.py --symbols {symbol} --days 180 --bar-size 1m --fill-mode backfill")
            print(f"  python tool/load_hist_multi.py --symbols {symbol} --days 180 --bar-size 5m --fill-mode backfill")
            print()

        print("To compute features (if bars exist but features missing):")
        for symbol in symbols:
            print(f"  python services/market/features/feature_calculator.py --symbol {symbol} --start {start_date} --end {end_date}")
        print()
        return False


def main():
    parser = argparse.ArgumentParser(description="Check data availability for backtest")
    parser.add_argument("--symbols", type=str, nargs="+", required=True, help="Symbols to check")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Number of days (from today backwards)")

    args = parser.parse_args()

    # Determine date range
    if args.days:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    elif args.start and args.end:
        start_date = args.start
        end_date = args.end
    else:
        # Default: last 30 days
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    success = check_data_availability(args.symbols, start_date, end_date)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
