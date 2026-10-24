#!/usr/bin/env python3
"""
Backfill Enhanced Features to ClickHouse - Phase 1

Computes 50 enhanced features for historical data and updates trading.features_1m table.
Processes in batches to avoid memory issues.

Usage:
    python tools/backfill_enhanced_features.py --symbols AAPL,TSLA --start 2025-08-01 --end 2025-11-05 --batch-days 7
"""

import argparse
import sys
from datetime import datetime, timedelta
from typing import List
import pandas as pd
from clickhouse_driver import Client

# Add project root to path
sys.path.insert(0, '.')

from libs.features.feature_engineer_v2 import FeatureEngineerV2


def fetch_vix_series(client: Client, start: str, end: str) -> pd.DataFrame:
    """Fetch VIX reference series from refs_vix_ticks and return as 1D DataFrame."""
    query = """
        SELECT ts, vix
        FROM trading.refs_vix_ticks
        WHERE ts >= %(st)s
          AND ts < %(et)s
        ORDER BY ts
    """
    rows = client.execute(query, {"st": start, "et": end})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "vix"])
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts")
    return df


def fetch_base_features(client: Client, symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch base features from ClickHouse"""
    query = """
        SELECT ts_start, symbol, open, high, low, close, volume
        FROM trading.bars_1m_ibkr
        WHERE symbol = %(sym)s
          AND ts_start >= %(st)s
          AND ts_start < %(et)s
        ORDER BY ts_start
    """

    rows = client.execute(query, {
        'sym': symbol,
        'st': start,
        'et': end
    })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        'ts_start', 'symbol', 'open', 'high', 'low', 'close', 'volume'
    ])
    df = df.rename(columns={'ts_start': 'ts'})
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.set_index('ts')

    return df


import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def insert_enhanced_features(
    client: Client,
    df_enhanced: pd.DataFrame,
    new_cols: List[str]
) -> int:
    """Insert enhanced features into ClickHouse using a temporary table and INSERT ... SELECT"""

    if df_enhanced.empty or not new_cols:
        logging.info("No new enhanced features to insert.")
        return 0

    # Create a temporary table
    temp_table_name = f"temp_features_{int(time.time())}"
    logging.info(f"Creating temporary table: {temp_table_name}")
    client.execute(f"CREATE TABLE {temp_table_name} AS trading.features_1m ENGINE = Memory")

    # Insert computed features into the temporary table
    df_insert = df_enhanced.reset_index()
    cols_to_insert = ['ts', 'symbol'] + [c for c in new_cols if c in df_insert.columns]
    df_insert = df_insert[cols_to_insert].dropna(subset=['ts'])
    
    if df_insert.empty:
        logging.info("No rows to insert after dropping NaNs.")
        return 0

    logging.info(f"Inserting {len(df_insert)} rows into temporary table.")
    client.insert_df(temp_table_name, df_insert)

    # Merge the temporary table into the main features table
    insert_cols = ", ".join(cols_to_insert)
    logging.info(f"Merging {len(df_insert)} rows from temporary table into trading.features_1m.")
    client.execute(f"INSERT INTO trading.features_1m ({insert_cols}) SELECT {insert_cols} FROM {temp_table_name}")

    # Clean up the temporary table
    logging.info(f"Dropping temporary table: {temp_table_name}")
    client.execute(f"DROP TABLE {temp_table_name}")

    return len(df_insert)




def backfill_symbol(
    client: Client,
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    batch_days: int = 7,
    force: bool = False,
) -> dict:
    """Backfill enhanced features for a single symbol"""

    logging.info(f"Processing symbol: {symbol}")

    new_cols = [
        'ret_5m', 'ret_15m', 'ret_1h', 'vol_5m', 'vol_15m', 'vol_1h',
        'rsi_14_5m', 'rsi_14_15m', 'ema_9_5m', 'ema_9_15m', 'ema_cross_1m', 'ema_cross_5m',
        'bb_position', 'bb_width', 'macd_hist',
        'ret_vs_spy_1m', 'ret_vs_spy_5m', 'ret_vs_spy_1h', 'vol_vs_spy',
        'rank_vs_spy', 'ret_vs_sector', 'volume_rank_20', 'price_rank_20',
        'corr_spy_20', 'beta_spy_20',
        'vwap_dist', 'vwap_dist_5m', 'high_low_range', 'trade_imb_5m',
        'volume_surge', 'price_momentum_5m', 'reversion_1m', 'volatility_ratio',
        'realized_vol_5m', 'realized_vol_15m', 'vol_ratio', 'atr_percentile_20',
        'range_ratio', 'close_position', 'gap_from_prev',
        'hour', 'minute', 'minutes_from_open', 'minutes_to_close',
        'is_lunch_time', 'is_monday', 'is_friday'
    ]

    stats = {
        'symbol': symbol,
        'batches_processed': 0,
        'rows_updated': 0,
        'errors': 0
    }

    current = start_date
    while current < end_date:
        batch_end = min(current + timedelta(days=batch_days), end_date)
        logging.info(f"Processing batch from {current.strftime('%Y-%m-%d')} to {batch_end.strftime('%Y-%m-%d')}")

        try:
            df = fetch_base_features(
                client, symbol,
                current.strftime('%Y-%m-%d %H:%M:%S'),
                batch_end.strftime('%Y-%m-%d %H:%M:%S')
            )

            if df.empty:
                logging.info(f"No data for {symbol} in this batch, skipping...")
                current = batch_end
                continue
            logging.info(f"Loaded {len(df)} rows for {symbol}")

            spy_df = None
            if symbol != 'SPY':
                spy_df = fetch_base_features(
                    client, 'SPY',
                    current.strftime('%Y-%m-%d %H:%M:%S'),
                    batch_end.strftime('%Y-%m-%d %H:%M:%S')
                )
                if spy_df.empty:
                    logging.warning("No SPY data, using fallback for relative features")
                    spy_df = None
                else:
                    logging.info(f"Loaded {len(spy_df)} SPY rows")

            vix_df = fetch_vix_series(
                client,
                current.strftime('%Y-%m-%d %H:%M:%S'),
                batch_end.strftime('%Y-%m-%d %H:%M:%S'),
            )
            if vix_df.empty:
                logging.warning("No VIX data found in refs_vix_ticks for this batch")

            eng = FeatureEngineerV2()
            df_enhanced = eng.compute_all_features(df, spy_df, vix_df)
            logging.info(f"Computed {len(df_enhanced.columns)} total features")

            rows_updated = insert_enhanced_features(
                client,
                df_enhanced,
                new_cols
            )
            logging.info(f"Updated {rows_updated} rows for {symbol} in this batch")

            stats['batches_processed'] += 1
            stats['rows_updated'] += rows_updated

        except Exception as e:
            logging.error(f"Error processing batch for {symbol}: {e}")
            stats['errors'] += 1

        current = batch_end

    return stats


def main():
    parser = argparse.ArgumentParser(description='Backfill enhanced features to ClickHouse')
    parser.add_argument('--symbols', required=True, help='Comma-separated symbols (e.g., AAPL,TSLA,NVDA)')
    parser.add_argument('--start', required=True, help='Start date YYYY-MM-DD')
    parser.add_argument('--end', required=True, help='End date YYYY-MM-DD')
    parser.add_argument('--batch-days', type=int, default=7, help='Batch size in days (default: 7)')
    parser.add_argument(
        '--only-cols',
        help='Optional comma-separated list of columns to update (subset of known enhanced columns)',
        default=None,
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force update even when target columns are already non-NULL (default: only fill NULLs)',
    )
    parser.add_argument('--host', default='localhost', help='ClickHouse host')

    args = parser.parse_args()
    logging.info(f"Starting feature backfill with args: {args}")

    # Parse inputs
    symbols = [s.strip() for s in args.symbols.split(',')]
    start_date = datetime.fromisoformat(args.start)
    end_date = datetime.fromisoformat(args.end)
    only_cols = None
    if args.only_cols:
        only_cols = [c.strip() for c in args.only_cols.split(',') if c.strip()]

    logging.info(f"Symbols: {', '.join(symbols)}")
    logging.info(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    logging.info(f"Batch size: {args.batch_days} days")
    if only_cols:
        logging.info(f"Only columns: {', '.join(only_cols)}")

    # Connect to ClickHouse
    client = Client(host=args.host)

    # Process each symbol
    all_stats = []
    for symbol in symbols:
        stats = backfill_symbol(
            client,
            symbol,
            start_date,
            end_date,
            args.batch_days,
            force=bool(args.force),
        )
        all_stats.append(stats)

    # Print summary
    logging.info("BACKFILL SUMMARY")
    total_rows = sum(s['rows_updated'] for s in all_stats)
    total_errors = sum(s['errors'] for s in all_stats)
    logging.info(f"Total rows updated: {total_rows}")
    logging.info(f"Total errors: {total_errors}")

    if total_errors > 0:
        logging.warning(f"Completed with {total_errors} errors")
        return 1
    else:
        logging.info("Backfill completed successfully!")
        return 0



if __name__ == "__main__":
    sys.exit(main())
