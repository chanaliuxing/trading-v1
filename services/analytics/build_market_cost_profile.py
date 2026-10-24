#!/usr/bin/env python3
"""
Daily Market Cost Profile Builder
Generates minute-of-day spread cost curves from BBO 1s features
Used for trading cost estimation and order placement thresholds
"""
from datetime import date, timedelta
from libs.db.ch_pool import get_client
from libs.logger import get_logger

log = get_logger(__name__)

SQL_INSERT_INTRADAY = """
INSERT INTO lake.market_cost_intraday_minute
SELECT
  symbol,
  toDate(ts) AS d,
  (toHour(ts)*60 + toMinute(ts)) AS minute_of_day,
  quantileTDigestIf(0.95)(spread_bps, is_valid=1) AS p95_spread_bps,
  quantileTDigestIf(0.50)(spread_bps, is_valid=1) AS p50_spread_bps,
  avgIf(spread_bps, is_valid=1) AS avg_spread_bps,
  countIf(is_valid=1) AS samples,
  now() AS updated_at
FROM lake.bbo_1s_features
WHERE d = %(day)s
GROUP BY symbol, d, minute_of_day
"""


def run(day: date | None = None):
    """
    Build market cost profile for a specific day

    Args:
        day: Trading day to process (defaults to yesterday)
    """
    d = day or (date.today() - timedelta(days=1))
    db = get_client()  # Use global connection pool

    try:
        log.info("Building market_cost_intraday_minute", day=str(d))

        # ClickHouseDB may use query() or execute()
        try:
            db.execute(SQL_INSERT_INTRADAY, {"day": d})
        except AttributeError:
            db.query(SQL_INSERT_INTRADAY, {"day": d})

        # Verify insertion
        verify_sql = "SELECT COUNT(*) as cnt FROM lake.market_cost_intraday_minute WHERE d = %(day)s"
        try:
            result = db.execute(verify_sql, {"day": d})
        except AttributeError:
            result = db.query(verify_sql, {"day": d})

        count = result[0]['cnt'] if result else 0
        log.info("Market cost profile built successfully", day=str(d), records=count)

    except Exception as e:
        log.error("Failed to build market cost profile", day=str(d), error=str(e))
        raise


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # Allow specific date: python build_market_cost_profile.py 2025-10-07
        target_date = date.fromisoformat(sys.argv[1])
        run(target_date)
    else:
        # Default: yesterday
        run()
