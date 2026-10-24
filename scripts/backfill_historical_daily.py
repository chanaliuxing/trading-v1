#!/usr/bin/env python3
"""
Backfill Historical Daily Bars
-------------------------------
Backfill missing daily bars for a date range.

Usage:
    # Backfill last 30 days for all universe symbols
    python scripts/backfill_historical_daily.py --days 30

    # Backfill specific date range
    python scripts/backfill_historical_daily.py --start 2025-09-01 --end 2025-10-09

    # Backfill specific symbols
    python scripts/backfill_historical_daily.py --days 7 --symbols AAPL,NVDA,TSLA

    # Dry run (show what would be backfilled without sending requests)
    python scripts/backfill_historical_daily.py --days 30 --dry-run
"""

import asyncio
import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

from aiokafka import AIOKafkaProducer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from libs.config import config
from libs.logger import get_logger

logger = get_logger(__name__)

NY_TZ = ZoneInfo("America/New_York")


async def backfill_date_range(
    start_date: date,
    end_date: date,
    symbols: List[str],
    *,
    request_topic: str = "md.backfill.request.1d.v1",
    bar_size: str = "1 day",
    use_rth: bool = True,
    dry_run: bool = False,
) -> None:
    """Backfill daily bars for a date range"""

    # Initialize Kafka producer
    kafka_cfg = getattr(config, "kafka_config", {}) or {}
    bootstrap = kafka_cfg.get("bootstrap_servers")

    if not bootstrap:
        raise RuntimeError("Kafka bootstrap_servers not configured")

    producer = None
    if not dry_run:
        producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda v: v.encode("utf-8") if isinstance(v, str) else v,
            linger_ms=5,
        )
        await producer.start()

    try:
        current = start_date
        total_requests = 0
        skipped_weekends = 0

        while current <= end_date:
            # Skip weekends (Saturday=5, Sunday=6)
            if current.weekday() >= 5:
                skipped_weekends += 1
                current += timedelta(days=1)
                continue

            # Full calendar day window: 00:00:00 → 00:00:00 next day (exact 86400 seconds)
            start_dt = datetime.combine(current, time(0, 0, 0), tzinfo=NY_TZ)
            end_dt = start_dt + timedelta(days=1)

            start_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.timestamp())
            duration_sec = end_ts - start_ts

            logger.info(
                f"Backfilling date: {current}",
                symbols=len(symbols),
                duration_sec=duration_sec,
                dry_run=dry_run,
            )

            for symbol in symbols:
                payload = {
                    "data": {
                        "symbol": symbol,
                        "from_ts": start_ts,
                        "to_ts": end_ts,
                        "bar_size": bar_size,
                        "use_rth": use_rth,
                        "reason": "historical_daily",
                    }
                }

                if dry_run:
                    print(f"  [DRY RUN] Would request: {symbol} {current} ({start_ts} → {end_ts})")
                else:
                    await producer.send(request_topic, key=symbol, value=payload)
                    logger.debug(f"Requested: {symbol} {current}")

                total_requests += 1

                # Throttle to avoid overwhelming IBKR API
                if not dry_run:
                    await asyncio.sleep(0.1)  # 100ms between requests

            current += timedelta(days=1)

        logger.info(
            "Historical daily backfill complete",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            total_requests=total_requests,
            skipped_weekends=skipped_weekends,
            dry_run=dry_run,
        )

    finally:
        if producer:
            await producer.stop()


def parse_date(date_str: str) -> date:
    """Parse date string in YYYY-MM-DD format"""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


async def main():
    parser = argparse.ArgumentParser(description="Backfill historical daily bars")
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
        "--symbols",
        type=str,
        help="Comma-separated list of symbols (default: config.universe)",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default="md.backfill.request.1d.v1",
        help="Kafka topic for backfill requests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be backfilled without sending requests",
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

    if end_date >= today:
        logger.warning(f"End date {end_date} is today or future, capping to yesterday {yesterday}")
        end_date = yesterday

    # Determine symbols
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        symbols = getattr(config, "universe", [])
        if not symbols:
            parser.error("No symbols configured in config.universe and --symbols not provided")

    logger.info(
        "Historical daily backfill starting",
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        symbols=len(symbols),
        symbol_list=symbols[:5] if len(symbols) > 5 else symbols,
        dry_run=args.dry_run,
    )

    await backfill_date_range(
        start_date=start_date,
        end_date=end_date,
        symbols=symbols,
        request_topic=args.topic,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    asyncio.run(main())
