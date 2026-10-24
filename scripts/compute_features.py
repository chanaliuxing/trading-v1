#!/usr/bin/env python3
"""
Compute Features for Backfilled Bars
Standalone script to calculate features for all IBKR resolutions after backfill

Usage:
  # Compute features for last 3 days (all resolutions):
  python3 scripts/compute_features.py --days 3

  # Compute features for specific resolution:
  python3 scripts/compute_features.py --res 1m --days 7

  # Compute for specific date range:
  python3 scripts/compute_features.py --res 5m --start 2025-10-01 --end 2025-10-05

  # Compute for specific symbols:
  python3 scripts/compute_features.py --res 1m --days 3 --symbols AAPL,TSLA,MSFT
"""
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import argparse
import asyncio
from datetime import datetime, timedelta, date
from typing import List, Optional
from zoneinfo import ZoneInfo

from services.market.features.feature_calculator import FeatureCalculator
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)
NY_TZ = ZoneInfo(getattr(config, "timezone", "America/New_York"))

# All supported IBKR resolutions (excluding 5s/10s which are live-only)
ALL_RESOLUTIONS = ["1m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "4h", "1d"]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compute features for backfilled bar data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--res",
        default="all",
        help="Resolution(s) to compute: 1m, 5m, 10m, 15m, 30m, 2h, 3h, 4h, 1d, or 'all' (default: all)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Number of days to look back (default: 3)"
    )
    parser.add_argument(
        "--start",
        default="",
        help="Start date YYYY-MM-DD (overrides --days)"
    )
    parser.add_argument(
        "--end",
        default="",
        help="End date YYYY-MM-DD (overrides --days)"
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Comma-separated list of symbols (default: all symbols in config.universe)"
    )
    return parser.parse_args()


async def compute_features_for_resolution(
    resolution: str,
    start_time: datetime,
    end_time: datetime,
    symbols: Optional[List[str]] = None
):
    """Compute features for a single resolution"""
    logger.info(
        f"Computing features for {resolution}",
        resolution=resolution,
        start=str(start_time),
        end=str(end_time),
        symbols=len(symbols) if symbols else "all"
    )

    try:
        calc = FeatureCalculator(resolution=resolution)
        await calc.start()  # Initialize ClickHouse connection
        await calc.compute_features_for_range(start_time, end_time, symbols)
        await calc.stop()  # Cleanup
        logger.info(f"✅ Features computed successfully", resolution=resolution)
        return True
    except Exception as e:
        logger.error(f"❌ Failed to compute features", resolution=resolution, error=str(e))
        return False


async def main():
    args = parse_args()

    # Parse date range
    if args.start and args.end:
        start_date = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end)
    else:
        now_ny = datetime.now(NY_TZ)
        end_date = now_ny.date()
        start_date = end_date - timedelta(days=args.days - 1)

    # Convert to datetime (RTH: 9:30 AM - 4:00 PM ET)
    start_time = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=None)
    end_time = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).replace(tzinfo=None)

    # Parse symbols
    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        # Use config universe if available
        symbols = getattr(config, 'universe', None)

    # Parse resolutions
    if args.res.lower() == "all":
        resolutions = ALL_RESOLUTIONS
    else:
        resolutions = [r.strip() for r in args.res.split(",") if r.strip()]

    logger.info(
        "Feature computation started",
        resolutions=resolutions,
        start_date=str(start_date),
        end_date=str(end_date),
        symbols=len(symbols) if symbols else "all"
    )

    # Compute features for each resolution
    results = {}
    for res in resolutions:
        try:
            success = await compute_features_for_resolution(res, start_time, end_time, symbols)
            results[res] = "✅ Success" if success else "❌ Failed"
        except Exception as e:
            logger.error(f"Error processing resolution", resolution=res, error=str(e))
            results[res] = f"❌ Error: {str(e)}"

    # Summary
    print("\n" + "="*60)
    print("FEATURE COMPUTATION SUMMARY")
    print("="*60)
    print(f"Date range: {start_date} to {end_date}")
    print(f"Symbols: {len(symbols) if symbols else 'all'}")
    print("-"*60)
    for res, status in results.items():
        print(f"  {res:6s} : {status}")
    print("="*60)

    success_count = sum(1 for s in results.values() if "Success" in s)
    print(f"\n✅ {success_count}/{len(results)} resolutions completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
