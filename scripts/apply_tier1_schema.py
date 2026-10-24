#!/usr/bin/env python3
"""
Apply Tier-1 feature schema updates to all feature tables
Adds all missing columns required for comprehensive feature set
"""
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger

logger = get_logger(__name__)

# All feature tables
FEATURE_TABLES = [
    "features_5s",
    "features_1m",
    "features_5m",
    "features_10m",
    "features_15m",
    "features_30m",
    "features_2h",
    "features_3h",
    "features_4h",
    "features_1d",
]

# New columns to add (column_name, type, comment)
NEW_COLUMNS = [
    # EMA features
    ("ema_9", "Nullable(Float64)", "EMA 9-period"),
    ("ema_21", "Nullable(Float64)", "EMA 21-period"),
    ("ema_cross", "Nullable(Float64)", "EMA cross (ema_9 - ema_21)"),

    # Volatility
    ("vix_diff", "Nullable(Float64)", "VIX - VIX_SMA_20"),
    ("atr_20", "Nullable(Float64)", "ATR 20-period"),

    # Market context (SPY/QQQ)
    ("spy_ret_1", "Nullable(Float64)", "SPY 1-period return"),
    ("spy_ret_5", "Nullable(Float64)", "SPY 5-period return"),
    ("qqq_ret_1", "Nullable(Float64)", "QQQ 1-period return"),
    ("qqq_ret_5", "Nullable(Float64)", "QQQ 5-period return"),
    ("beta_spy_20", "Nullable(Float64)", "Rolling beta vs SPY (20-period)"),
    ("beta_spy_60", "Nullable(Float64)", "Rolling beta vs SPY (60-period)"),
    ("corr_spy_20", "Nullable(Float64)", "Rolling correlation with SPY (20-period)"),
    ("corr_spy_60", "Nullable(Float64)", "Rolling correlation with SPY (60-period)"),
    ("corr_qqq_20", "Nullable(Float64)", "Rolling correlation with QQQ (20-period)"),

    # Microstructure
    ("tick_imbalance", "Nullable(Float64)", "Trade tick imbalance ratio"),
    ("trade_count_ratio", "Nullable(Float64)", "Trade count / rolling avg"),
    ("avg_trade_size", "Nullable(Float64)", "Average trade size (volume/trades)"),
    ("price_impact_5m", "Nullable(Float64)", "5-minute price impact"),

    # Statistical features
    ("zscore_close_20", "Nullable(Float64)", "Close z-score (20-period)"),
    ("zscore_close_60", "Nullable(Float64)", "Close z-score (60-period)"),
    ("zscore_volume_20", "Nullable(Float64)", "Volume z-score (20-period)"),
    ("zscore_volume_60", "Nullable(Float64)", "Volume z-score (60-period)"),
    ("skew_20", "Nullable(Float64)", "Return skewness (20-period)"),
    ("skew_60", "Nullable(Float64)", "Return skewness (60-period)"),
    ("kurt_20", "Nullable(Float64)", "Return excess kurtosis (20-period)"),
    ("kurt_60", "Nullable(Float64)", "Return excess kurtosis (60-period)"),
    ("autocorr_ret_1", "Nullable(Float64)", "Return autocorrelation lag-1"),
    ("autocorr_ret_5", "Nullable(Float64)", "Return autocorrelation lag-5"),
    ("hurst_20", "Nullable(Float64)", "Hurst exponent (20-period R/S)"),

    # Temporal/Meta
    ("time_since_open", "Nullable(UInt16)", "Minutes since market open (9:30 ET)"),
    ("day_of_week", "Nullable(UInt8)", "Day of week (0=Mon, 4=Fri)"),
    ("hour_of_day", "Nullable(UInt8)", "Hour of day (0-23 ET)"),
    ("is_first_30min", "Nullable(UInt8)", "Flag: first 30min of session"),
    ("is_last_30min", "Nullable(UInt8)", "Flag: last 30min of session"),

    # Advanced
    ("kama_10", "Nullable(Float64)", "Kaufman Adaptive MA (10-period)"),
    ("entropy_20", "Nullable(Float64)", "Shannon entropy of returns (20-period)"),
]


def main():
    db = ClickHouseDB()

    logger.info("Starting Tier-1 feature schema migration")
    logger.info(f"Adding {len(NEW_COLUMNS)} new columns to {len(FEATURE_TABLES)} tables")

    for table in FEATURE_TABLES:
        logger.info(f"Updating table: trading.{table}")

        for col_name, col_type, col_comment in NEW_COLUMNS:
            try:
                sql = f"""
                ALTER TABLE trading.{table}
                ADD COLUMN IF NOT EXISTS {col_name} {col_type}
                COMMENT '{col_comment}'
                """
                db.execute(sql)
                logger.debug(f"  ✓ Added column: {col_name}")
            except Exception as e:
                logger.error(f"  ✗ Failed to add column {col_name}: {e}")

        logger.info(f"  ✓ Completed table: trading.{table}")

    logger.info("✅ Schema migration completed successfully!")

    # Verify
    logger.info("\nVerifying schema updates...")
    for table in FEATURE_TABLES[:2]:  # Check first 2 tables as sample
        sql = f"SELECT count(*) as col_count FROM system.columns WHERE database='trading' AND table='{table}'"
        result = db.query(sql)
        col_count = result[0]['col_count'] if result else 0
        logger.info(f"  {table}: {col_count} total columns")


if __name__ == "__main__":
    main()
