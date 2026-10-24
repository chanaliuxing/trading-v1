#!/usr/bin/env python3
"""
Backfill 'tick_imbalance' column in the features_5m table.

This script addresses a data quality issue where 'tick_imbalance' is all zeros
because the standard feature resampling process does not have access to the
high-frequency data required to compute it.

This tool fetches the real 'tick_imbalance' from the high-frequency
'sfs.sfs_5s' table, aggregates it into 5-minute buckets, and updates the
'trading.features_5m' table with the correct values.

Usage:
    python tools/backfill_tick_imbalance.py --start 2025-09-24 --end 2025-11-23 --batch-days 7
"""

import argparse
import sys
from datetime import datetime, timedelta
from typing import Dict

# Add project root to path
sys.path.insert(0, '.')

from libs.db.ch_pool import get_client

def backfill_tick_imbalance(start_date: datetime, end_date: datetime, batch_days: int = 7):
    """
    Fetches tick_imbalance from sfs.sfs_5s, aggregates it to 5-minute buckets,
    and updates the trading.features_5m table.
    """
    client = get_client()
    print("Connected to ClickHouse.")

    total_rows_updated = 0
    current_date = start_date
    while current_date < end_date:
        batch_start = current_date
        batch_end = min(current_date + timedelta(days=batch_days), end_date)
        
        print(f"\nProcessing batch: {batch_start.date()} to {batch_end.date()}...")

        # 1. Aggregate tick_imbalance from sfs.sfs_5s
        # This query calculates the average tick_imbalance for each 5-minute window.
        aggregation_query = f"""
        SELECT
            toStartOfInterval(ts, INTERVAL 5 MINUTE) AS ts_5m,
            symbol,
            avg(tick_imbalance) AS avg_tick_imbalance
        FROM sfs.sfs_5s
        WHERE ts >= '{batch_start.strftime('%Y-%m-%d %H:%M:%S')}'
          AND ts < '{batch_end.strftime('%Y-%m-%d %H:%M:%S')}'
        GROUP BY symbol, ts_5m
        HAVING isFinite(avg_tick_imbalance)
        """
        
        try:
            aggregated_data = client.query_df(aggregation_query)
            if aggregated_data.empty:
                print("  No tick_imbalance data found in sfs.sfs_5s for this batch. Skipping.")
                current_date = batch_end
                continue
            
            print(f"  Found {len(aggregated_data)} aggregated 5-minute buckets to update.")

            # 2. Update trading.features_5m using the aggregated data
            # We use a JOIN in the ALTER TABLE UPDATE statement for efficiency.
            
            # Create a temporary table to hold the aggregated data
            temp_table_name = f"temp_imbalance_backfill_{batch_start.strftime('%Y%m%d')}"
            client.command(f"DROP TABLE IF EXISTS {temp_table_name}")
            
            client.insert_df(
                table=temp_table_name,
                df=aggregated_data,
                settings=dict(use_numpy=True),
                database='default' # Or a specific temp DB if you have one
            )
            print(f"  Created temporary table '{temp_table_name}' with aggregated data.")

            # Perform the update via JOIN
            update_query = f"""
            ALTER TABLE trading.features_5m
            UPDATE tick_imbalance = (
                SELECT avg_tick_imbalance
                FROM {temp_table_name}
                WHERE {temp_table_name}.symbol = trading.features_5m.symbol
                  AND {temp_table_name}.ts_5m = trading.features_5m.ts
            )
            WHERE symbol IN (SELECT DISTINCT symbol FROM {temp_table_name})
              AND ts >= '{batch_start.strftime('%Y-%m-%d %H:%M:%S')}'
              AND ts < '{batch_end.strftime('%Y-%m-%d %H:%M:%S')}'
            """
            
            print("  Executing update on trading.features_5m...")
            # This is an async mutation, we need to wait for it to finish
            client.command(update_query, settings={'mutations_sync': 2})
            
            # Since we can't get the exact number of rows updated from the mutation,
            # we'll count the rows that now have non-zero tick_imbalance as a proxy.
            verification_query = f"""
            SELECT count() FROM trading.features_5m
            WHERE symbol IN (SELECT DISTINCT symbol FROM {temp_table_name})
              AND ts >= '{batch_start.strftime('%Y-%m-%d %H:%M:%S')}'
              AND ts < '{batch_end.strftime('%Y-%m-%d %H:%M:%S')}'
              AND tick_imbalance != 0
            """
            rows_updated_in_batch = client.query_scalar(verification_query)
            total_rows_updated += rows_updated_in_batch

            print(f"  âœ Heavy_check_mark: Batch complete. Updated approximately {rows_updated_in_batch} rows.")

            # Clean up the temporary table
            client.command(f"DROP TABLE {temp_table_name}")

        except Exception as e:
            print(f"  âœ  ERROR processing batch: {e}")
            import traceback
            traceback.print_exc()

        current_date = batch_end

    print("\n" + "="*50)
    print("Backfill Summary")
    print("="*50)
    print(f"Total rows updated with non-zero tick_imbalance: {total_rows_updated}")
    print("âœ Heavy_check_mark: Backfill process completed.")

def main():
    parser = argparse.ArgumentParser(description="Backfill 'tick_imbalance' from sfs.sfs_5s to trading.features_5m.")
    parser.add_argument('--start', required=True, help='Start date YYYY-MM-DD')
    parser.add_argument('--end', required=True, help='End date YYYY-MM-DD')
    parser.add_argument('--batch-days', type=int, default=7, help='Number of days to process in each batch (default: 7)')
    
    args = parser.parse_args()

    start_date = datetime.fromisoformat(args.start)
    end_date = datetime.fromisoformat(args.end)

    backfill_tick_imbalance(start_date, end_date, args.batch_days)

if __name__ == "__main__":
    sys.exit(main())
