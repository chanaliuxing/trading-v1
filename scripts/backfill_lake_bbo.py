#!/usr/bin/env python3
"""
Backfill Lake BBO Data
----------------------
Backfill lake.bbo_1s from lake.quotes_l1 historical data.

The materialized view mv_quotes_to_bbo_1s only processes NEW inserts,
so this script backfills historical data by manually aggregating existing quotes.

Usage:
    # Backfill last 30 days
    python scripts/backfill_lake_bbo.py --days 30

    # Backfill specific date range
    python scripts/backfill_lake_bbo.py --start 2025-09-01 --end 2025-10-09

    # Dry run (show what would be done)
    python scripts/backfill_lake_bbo.py --days 7 --dry-run
"""

import asyncio
import argparse
from datetime import date, datetime, timedelta
from typing import Optional

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)


async def backfill_bbo_1s(
    start_date: date,
    end_date: date,
    *,
    dry_run: bool = False,
) -> None:
    """Backfill BBO 1s data from quotes_l1"""

    db = ClickHouseDB()
    await db.init_async_client()

    try:
        current = start_date
        total_days = 0
        total_rows = 0

        while current <= end_date:
            logger.info(f"Backfilling BBO 1s for date: {current}", dry_run=dry_run)

            if dry_run:
                # Just count what would be inserted
                count_sql = """
                SELECT count(*) as cnt
                FROM (
                    SELECT
                        symbol,
                        toStartOfSecond(ts) AS ts
                    FROM lake.quotes_l1
                    WHERE d = %(date)s
                    GROUP BY symbol, ts
                )
                """
                result = db.query(count_sql, {"date": current})
                row_count = result[0]['cnt'] if result else 0
                print(f"  [DRY RUN] Would insert {row_count:,} rows for {current}")
                total_rows += row_count
            else:
                # Insert aggregated data
                insert_sql = """
                INSERT INTO lake.bbo_1s (symbol, ts, bid, ask, bid_size, ask_size, ver)
                SELECT
                    symbol,
                    toStartOfSecond(ts) AS ts,
                    argMax(bid, ver) AS bid,
                    argMax(ask, ver) AS ask,
                    argMax(bid_size, ver) AS bid_size,
                    argMax(ask_size, ver) AS ask_size,
                    toUnixTimestamp(now()) * 1000 AS ver
                FROM lake.quotes_l1
                WHERE d = %(date)s
                GROUP BY symbol, ts
                """

                db.execute(insert_sql, {"date": current})
                logger.info(f"Completed backfill for {current}")

            total_days += 1
            current += timedelta(days=1)

        if dry_run:
            logger.info(
                f"Dry run complete - would backfill {total_rows:,} rows across {total_days} days",
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
        else:
            # Verify final result
            count_sql = "SELECT count(*) as cnt FROM lake.bbo_1s"
            result = db.query(count_sql)
            final_count = result[0]['cnt'] if result else 0

            logger.info(
                f"Backfill complete",
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                days_processed=total_days,
                total_bbo_rows=final_count,
            )

            # Show sample data
            sample_sql = """
            SELECT symbol, toDate(ts) as d, count(*) as rows
            FROM lake.bbo_1s
            GROUP BY symbol, d
            ORDER BY d DESC, symbol
            LIMIT 10
            """
            sample = db.query(sample_sql)
            logger.info("Sample data from bbo_1s:", sample=sample)

    finally:
        await db.close_async()


def parse_date(date_str: str) -> date:
    """Parse date string in YYYY-MM-DD format"""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


async def main():
    parser = argparse.ArgumentParser(description="Backfill lake.bbo_1s from lake.quotes_l1")
    parser.add_argument(
        "--days",
        type=int,
        help="Number of days back to backfill (from today)",
    )
    parser.add_argument(
        "--start",
        type=str,
        help="Start date (YYYY-MM-DD format)",
    )
    parser.add_argument(
        "--end",
        type=str,
        help="End date (YYYY-MM-DD format, default: yesterday)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be backfilled without inserting data",
    )

    args = parser.parse_args()

    # Determine date range
    if args.days and (args.start or args.end):
        parser.error("Cannot specify both --days and --start/--end")

    today = date.today()
    yesterday = today - timedelta(days=1)

    if args.days:
        end_date = yesterday
        start_date = yesterday - timedelta(days=args.days - 1)
    else:
        if not args.start:
            parser.error("Must specify either --days or --start")
        start_date = parse_date(args.start)
        end_date = parse_date(args.end) if args.end else yesterday

    # Validate date range
    if start_date > end_date:
        parser.error(f"Start date {start_date} is after end date {end_date}")

    logger.info(
        "Lake BBO backfill starting",
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        days=(end_date - start_date).days + 1,
        dry_run=args.dry_run,
    )

    await backfill_bbo_1s(
        start_date=start_date,
        end_date=end_date,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    asyncio.run(main())
