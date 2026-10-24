#!/usr/bin/env python3
"""
Aggregate today's 5s bars into 1m and 5m bars
This bypasses the backfill queue and immediately populates bars_1m_agg and bars_5m_agg
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger
from datetime import datetime

logger = get_logger(__name__)

def main():
    db = ClickHouseDB()

    logger.info("Starting aggregation of today's bars...")

    # Aggregate 5s → 1m bars
    logger.info("Aggregating 5s → 1m bars...")
    sql_1m = """
    INSERT INTO trading.bars_1m
    WITH aggregated AS (
        SELECT
            symbol,
            toStartOfMinute(ts_start) as minute_start,
            argMin(open, ts_start) as first_open,
            max(high) as max_high,
            min(low) as min_low,
            argMax(close, ts_start) as last_close,
            sum(close * volume) as total_close_vol,
            sum(volume) as total_volume,
            sum(trades) as total_trades
        FROM trading.bars_5s
        WHERE date(ts_start) = today()
          AND is_rth = 1
        GROUP BY symbol, minute_start
    )
    SELECT
        symbol,
        60 as bar_sec,
        minute_start as ts_start,
        minute_start + INTERVAL 60 SECOND as ts_end,
        first_open as open,
        max_high as high,
        min_low as low,
        last_close as close,
        total_close_vol / nullIf(total_volume, 0) as vwap,
        total_volume as volume,
        total_trades as trades,
        NULL as bid_last,
        NULL as ask_last,
        last_close as mid_last,
        'TRADES' as what_to_show,
        1 as is_rth,
        'IBKR' as source,
        'SMART' as exchange,
        'USD' as currency,
        'America/New_York' as tz,
        toDate(minute_start) as d,
        (toHour(minute_start) * 60) + toMinute(minute_start) as minute_of_day,
        toUnixTimestamp(now()) as ver,
        0 as is_backfill,
        now() as created_at,
        now64(3) as ingest_ts
    FROM aggregated
    """

    try:
        db.execute(sql_1m)
        logger.info("✅ 1m aggregation complete")
    except Exception as e:
        logger.error(f"Failed to aggregate 1m bars: {e}")
        return 1

    # Aggregate 5s → 5m bars
    logger.info("Aggregating 5s → 5m bars...")
    sql_5m = """
    INSERT INTO trading.bars_5m
    WITH aggregated AS (
        SELECT
            symbol,
            toStartOfFiveMinutes(ts_start) as fivemin_start,
            argMin(open, ts_start) as first_open,
            max(high) as max_high,
            min(low) as min_low,
            argMax(close, ts_start) as last_close,
            sum(close * volume) as total_close_vol,
            sum(volume) as total_volume,
            sum(trades) as total_trades
        FROM trading.bars_5s
        WHERE date(ts_start) = today()
          AND is_rth = 1
        GROUP BY symbol, fivemin_start
    )
    SELECT
        symbol,
        300 as bar_sec,
        fivemin_start as ts_start,
        fivemin_start + INTERVAL 300 SECOND as ts_end,
        first_open as open,
        max_high as high,
        min_low as low,
        last_close as close,
        total_close_vol / nullIf(total_volume, 0) as vwap,
        total_volume as volume,
        total_trades as trades,
        NULL as bid_last,
        NULL as ask_last,
        last_close as mid_last,
        toUnixTimestamp(now()) as ver,
        0 as is_backfill,
        now() as created_at,
        'TRADES' as what_to_show,
        1 as is_rth,
        'IBKR' as source,
        'SMART' as exchange,
        'USD' as currency,
        'America/New_York' as tz,
        toDate(fivemin_start) as d,
        (toHour(fivemin_start) * 60) + toMinute(fivemin_start) as minute_of_day,
        now64(3) as ingest_ts
    FROM aggregated
    """

    try:
        db.execute(sql_5m)
        logger.info("✅ 5m aggregation complete")
    except Exception as e:
        logger.error(f"Failed to aggregate 5m bars: {e}")
        return 1

    # Verify results
    logger.info("\nVerifying results...")
    count_1m = db.query("SELECT COUNT(*) as cnt FROM trading.bars_1m WHERE date(ts_start)=today()")[0]['cnt']
    count_5m = db.query("SELECT COUNT(*) as cnt FROM trading.bars_5m WHERE date(ts_start)=today()")[0]['cnt']

    logger.info(f"1m bars aggregated: {count_1m}")
    logger.info(f"5m bars aggregated: {count_5m}")

    # Show sample for TSLA
    tsla_1m = db.query("""
        SELECT symbol, COUNT(*) as bars, MIN(ts_start) as first, MAX(ts_start) as last
        FROM trading.bars_1m_current
        WHERE symbol='TSLA' AND date(ts_start)=today()
        GROUP BY symbol
    """)

    if tsla_1m:
        logger.info(f"\n✅ TSLA 1m bars now available: {tsla_1m[0]['bars']} bars from {tsla_1m[0]['first']} to {tsla_1m[0]['last']}")
    else:
        logger.warning("\n⚠️  No TSLA 1m bars found in bars_1m_current")

    logger.info("\n✅ Aggregation complete! Strategies should now be able to access today's 1m and 5m bars.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
