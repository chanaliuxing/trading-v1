#!/usr/bin/env python3
"""
Verify data dependencies for HedgingService.

Checks:
1. VIX historical data (252 days for percentile calculation)
2. SPY historical data (80 days for trend/ATR)
3. Portfolio exposure data (signals_realtime table)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)


def check_vix_data():
    """Check VIX historical data availability."""
    ch = ClickHouseDB()

    sql = """
    SELECT
        count() as total_rows,
        min(ts_end) as earliest,
        max(ts_end) as latest,
        dateDiff('day', min(ts_end), max(ts_end)) as days_span
    FROM trading.bars_1d_ibkr
    WHERE symbol = 'VIX'
    """

    rows = ch.query(sql)
    if not rows:
        logger.error("❌ No VIX data found in trading.bars_1d_ibkr")
        return False

    row = rows[0]
    total = row.get("total_rows", 0)
    days_span = row.get("days_span", 0)
    earliest = row.get("earliest")
    latest = row.get("latest")

    logger.info(f"✅ VIX data: {total} rows, {days_span} days ({earliest} to {latest})")

    if days_span < 252:
        logger.warning(f"⚠️  VIX data only spans {days_span} days (need 252 for percentile)")
        return False

    return True


def check_spy_data():
    """Check SPY historical data availability."""
    ch = ClickHouseDB()

    sql = """
    SELECT
        count() as total_rows,
        min(ts_end) as earliest,
        max(ts_end) as latest,
        dateDiff('day', min(ts_end), max(ts_end)) as days_span
    FROM trading.bars_1d_ibkr
    WHERE symbol = 'SPY'
    """

    rows = ch.query(sql)
    if not rows:
        logger.error("❌ No SPY data found in trading.bars_1d_ibkr")
        return False

    row = rows[0]
    total = row.get("total_rows", 0)
    days_span = row.get("days_span", 0)
    earliest = row.get("earliest")
    latest = row.get("latest")

    logger.info(f"✅ SPY data: {total} rows, {days_span} days ({earliest} to {latest})")

    if days_span < 80:
        logger.warning(f"⚠️  SPY data only spans {days_span} days (need 80 for ATR)")
        return False

    return True


def check_positions_data():
    """Check if signals_realtime table exists and has recent data."""
    ch = ClickHouseDB()

    # Check if table exists
    sql = """
    SELECT count() as row_count
    FROM system.tables
    WHERE database = 'trading' AND name = 'signals_realtime'
    """

    rows = ch.query(sql)
    if not rows or rows[0].get("row_count", 0) == 0:
        logger.warning("⚠️  trading.signals_realtime table does not exist")
        logger.info("   This is OK - table will be populated once strategies start running")
        return True

    # Check recent data
    sql = """
    SELECT
        count() as total_rows,
        max(ts) as latest_ts,
        countDistinct(strategy_id) as strategy_count,
        countDistinct(symbol) as symbol_count
    FROM trading.signals_realtime
    WHERE ts >= now() - INTERVAL 1 DAY
    """

    rows = ch.query(sql)
    if not rows:
        logger.warning("⚠️  No recent data in signals_realtime")
        logger.info("   This is OK - data will appear once strategies start")
        return True

    row = rows[0]
    total = row.get("total_rows", 0)
    latest = row.get("latest_ts")
    strategies = row.get("strategy_count", 0)
    symbols = row.get("symbol_count", 0)

    logger.info(
        f"✅ Positions data: {total} rows, {strategies} strategies, "
        f"{symbols} symbols (latest: {latest})"
    )

    return True


def main():
    logger.info("=" * 60)
    logger.info("Verifying HedgingService Data Dependencies")
    logger.info("=" * 60)

    checks = [
        ("VIX Historical Data", check_vix_data),
        ("SPY Historical Data", check_spy_data),
        ("Positions Data", check_positions_data),
    ]

    results = []
    for name, check_fn in checks:
        logger.info(f"\n🔍 Checking: {name}")
        try:
            result = check_fn()
            results.append((name, result))
        except Exception as e:
            logger.error(f"❌ {name} check failed", error=str(e), exc_info=True)
            results.append((name, False))

    logger.info("\n" + "=" * 60)
    logger.info("Summary")
    logger.info("=" * 60)

    all_passed = True
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        logger.info(f"{status}: {name}")
        if not result:
            all_passed = False

    logger.info("=" * 60)

    if all_passed:
        logger.info("✅ All data dependencies verified!")
        logger.info("\nNext steps:")
        logger.info("1. Update ai_trader.yaml to enable hedging")
        logger.info("2. Add HedgingService to run_system.py")
        logger.info("3. Start system in dry-run mode")
        return 0
    else:
        logger.warning("⚠️  Some checks failed, but this may be OK if system hasn't started yet")
        return 1


if __name__ == "__main__":
    sys.exit(main())
