#!/usr/bin/env python3
"""Comprehensive ClickHouse data availability check for LLM trading system"""

import asyncio
from clickhouse_driver import Client
from datetime import datetime, timedelta
import json


async def main():
    print("="*100)
    print("CLICKHOUSE DATA AVAILABILITY CHECK")
    print("="*100)
    print()

    # Connect to ClickHouse
    try:
        client = Client(
            host='localhost',
            port=9000,
            user='default',
            password='',
            database='trading'
        )
        print("✅ Connected to ClickHouse")
    except Exception as e:
        print(f"❌ Failed to connect to ClickHouse: {e}")
        return

    # Get all tables
    print("\n" + "="*100)
    print("AVAILABLE TABLES")
    print("="*100)

    tables = client.execute("SHOW TABLES FROM trading")
    print(f"\nFound {len(tables)} tables in 'trading' database:")
    for table in tables:
        print(f"  - {table[0]}")

    # Check sfs database too
    try:
        sfs_tables = client.execute("SHOW TABLES FROM sfs")
        print(f"\nFound {len(sfs_tables)} tables in 'sfs' database:")
        for table in sfs_tables:
            print(f"  - {table[0]}")
    except:
        print("\n⚠️ No 'sfs' database found")

    # Define tables to check
    tables_to_check = [
        # Current known tables
        ("trading", "features_5s"),
        ("sfs", "sfs_5s"),
        ("sfs", "sfs_1m_tumble"),
        ("trading", "sfs_features"),

        # Multi-timeframe tables (may not exist)
        ("trading", "features_1m"),
        ("trading", "features_5m"),
        ("trading", "features_15m"),
        ("trading", "features_1h"),
        ("trading", "features_2h"),
        ("trading", "features_4h"),
        ("trading", "features_1d"),

        # Market-wide data
        ("trading", "refs_vix_ticks"),
        ("trading", "spy_features"),
        ("trading", "qqq_features"),

        # Bars data
        ("trading", "bars_1s"),
        ("trading", "bars_5s"),
        ("trading", "bars_1m"),
    ]

    print("\n" + "="*100)
    print("CHECKING TABLE DATA AVAILABILITY")
    print("="*100)

    for db, table in tables_to_check:
        print(f"\n{'='*100}")
        print(f"TABLE: {db}.{table}")
        print("="*100)

        try:
            # Check if table exists and get row count
            count_query = f"SELECT COUNT(*) FROM {db}.{table}"
            count = client.execute(count_query)[0][0]

            if count == 0:
                print(f"⚠️ Table exists but is EMPTY (0 rows)")
                continue

            print(f"✅ Table exists with {count:,} rows")

            # Get table schema
            schema_query = f"DESCRIBE {db}.{table}"
            schema = client.execute(schema_query)
            print(f"\nSchema ({len(schema)} columns):")
            for col in schema[:10]:  # Show first 10 columns
                print(f"  - {col[0]}: {col[1]}")
            if len(schema) > 10:
                print(f"  ... and {len(schema) - 10} more columns")

            # Get sample data (most recent)
            sample_query = f"SELECT * FROM {db}.{table} ORDER BY ts DESC LIMIT 1"
            try:
                sample = client.execute(sample_query)
                if sample:
                    print(f"\nMost recent record (ts):")
                    row = sample[0]
                    # Get column names
                    col_names = [col[0] for col in schema]

                    # Show timestamp
                    ts_idx = col_names.index('ts') if 'ts' in col_names else 0
                    print(f"  Timestamp: {row[ts_idx]}")

                    # Show symbol if exists
                    if 'symbol' in col_names:
                        sym_idx = col_names.index('symbol')
                        print(f"  Symbol: {row[sym_idx]}")

                    # Show some key fields
                    key_fields = ['open', 'high', 'low', 'close', 'volume', 'vwap',
                                 'ret_5s', 'ret_1m', 'atr', 'rsi', 'ema_9', 'vix',
                                 'spy_ret_1', 'qqq_ret_1', 'beta_spy', 'spread_bps']

                    print(f"\nSample data (key fields):")
                    for field in key_fields:
                        if field in col_names:
                            idx = col_names.index(field)
                            value = row[idx]
                            if isinstance(value, float):
                                print(f"  {field}: {value:.6f}")
                            else:
                                print(f"  {field}: {value}")
            except Exception as e:
                print(f"  ⚠️ Could not fetch sample: {e}")

            # Check date range
            date_query = f"""
            SELECT
                MIN(ts) as earliest,
                MAX(ts) as latest,
                COUNT(DISTINCT symbol) as num_symbols,
                COUNT(*) as total_rows
            FROM {db}.{table}
            """
            try:
                stats = client.execute(date_query)[0]
                print(f"\nData Range:")
                print(f"  Earliest: {stats[0]}")
                print(f"  Latest: {stats[1]}")
                print(f"  Symbols: {stats[2]}")
                print(f"  Total Rows: {stats[3]:,}")
            except Exception as e:
                print(f"  ⚠️ Could not get date range: {e}")

            # Check for today's data
            try:
                today_query = f"""
                SELECT COUNT(*)
                FROM {db}.{table}
                WHERE toDate(ts) = today()
                """
                today_count = client.execute(today_query)[0][0]
                print(f"\nToday's data: {today_count:,} rows")

                if today_count > 0:
                    # Get today's symbols
                    symbols_query = f"""
                    SELECT DISTINCT symbol
                    FROM {db}.{table}
                    WHERE toDate(ts) = today()
                    LIMIT 10
                    """
                    symbols = client.execute(symbols_query)
                    print(f"  Today's symbols: {', '.join([s[0] for s in symbols])}")
            except Exception as e:
                print(f"  ⚠️ Could not check today's data: {e}")

        except Exception as e:
            print(f"❌ Table does NOT exist or error: {e}")

    # Special check: What fields are available in features_5s?
    print("\n" + "="*100)
    print("DETAILED CHECK: trading.features_5s (CURRENT DATA SOURCE)")
    print("="*100)

    try:
        schema = client.execute("DESCRIBE trading.features_5s")
        print(f"\nAll columns in features_5s ({len(schema)} total):")

        # Group by category
        categories = {
            'Price': ['open', 'high', 'low', 'close', 'vwap'],
            'Volume/Trades': ['volume', 'trades', 'avg_trade_size', 'vwap_volume'],
            'Returns': ['ret_5s', 'ret_1m', 'ret_5m', 'ret_cumsum'],
            'Volatility': ['rv_20', 'atr', 'atr_pct', 'rvol'],
            'Technical': ['ema_9', 'ema_21', 'rsi', 'bb_pct', 'zscore'],
            'Market': ['vix', 'spy_ret_1', 'qqq_ret_1', 'beta_spy', 'corr_spy'],
            'Microstructure': ['spread_bps', 'tick_imbalance', 'close_to_mid_bps', 'price_impact'],
            'Other': []
        }

        for col_name, col_type in schema:
            categorized = False
            for cat, fields in categories.items():
                if any(field in col_name.lower() for field in fields):
                    if col_name not in categories[cat]:
                        categories[cat].append(col_name)
                    categorized = True
                    break
            if not categorized and col_name not in ['ts', 'symbol', 'date']:
                categories['Other'].append(col_name)

        for cat, cols in categories.items():
            if cols:
                print(f"\n{cat}:")
                for col in sorted(cols):
                    print(f"  - {col}")

    except Exception as e:
        print(f"❌ Could not analyze features_5s: {e}")

    # Check SFS features table
    print("\n" + "="*100)
    print("DETAILED CHECK: trading.sfs_features (SFS INTEGRATION)")
    print("="*100)

    try:
        count = client.execute("SELECT COUNT(*) FROM trading.sfs_features")[0][0]
        if count > 0:
            print(f"✅ Table has {count:,} rows")

            schema = client.execute("DESCRIBE trading.sfs_features")
            print(f"\nAll SFS columns ({len(schema)} total):")
            for col_name, col_type in schema:
                print(f"  - {col_name}: {col_type}")

            # Sample data
            sample = client.execute("SELECT * FROM trading.sfs_features ORDER BY ts DESC LIMIT 1")
            if sample:
                col_names = [col[0] for col in schema]
                row = sample[0]
                print(f"\nMost recent SFS data:")
                for i, (name, value) in enumerate(zip(col_names, row)):
                    if isinstance(value, float):
                        print(f"  {name}: {value:.6f}")
                    else:
                        print(f"  {name}: {value}")
        else:
            print(f"⚠️ Table exists but is EMPTY")
    except Exception as e:
        print(f"❌ Could not check sfs_features: {e}")

    # Summary and recommendations
    print("\n" + "="*100)
    print("SUMMARY & RECOMMENDATIONS")
    print("="*100)

    print("\n✅ AVAILABLE DATA:")
    available = []
    missing = []

    for db, table in tables_to_check:
        try:
            count = client.execute(f"SELECT COUNT(*) FROM {db}.{table}")[0][0]
            if count > 0:
                available.append(f"{db}.{table}")
            else:
                missing.append(f"{db}.{table} (exists but empty)")
        except:
            missing.append(f"{db}.{table} (does not exist)")

    for tbl in available:
        print(f"  ✅ {tbl}")

    print("\n❌ MISSING/EMPTY DATA:")
    for tbl in missing:
        print(f"  ❌ {tbl}")

    print("\n📋 RECOMMENDATIONS:")

    if "trading.features_5s" in available:
        print("  1. ✅ Use trading.features_5s for 5s data")

    if "trading.sfs_features" not in available:
        print("  2. ⚠️ trading.sfs_features is empty - run SFS service")

    if not any("features_5m" in t for t in available):
        print("  3. ⚠️ No 5m/15m/1h features - need to create aggregation tables")

    if not any("vix" in t for t in available):
        print("  4. ⚠️ No VIX data - need to add market-wide data source")

    print("\n" + "="*100)
    print("CHECK COMPLETE")
    print("="*100)


if __name__ == "__main__":
    asyncio.run(main())
