"""
ClickHouse Data Validator
=========================

This script validates the integrity of time-series data stored in the ClickHouse
database. It is designed to be extensible for various data types (bar sizes) and
can perform several crucial checks.

Key Validation Checks:
1.  **Data Presence**: Checks if any data exists for the specified symbol and day.
2.  **Duplicate Timestamps**: Ensures that there are no duplicate entries for the
    same timestamp and symbol.
3.  **OHLC Integrity**: Verifies that for each bar, the high is the maximum and
    the low is the minimum of the open, high, low, and close prices.
4.  **Timestamp Gaps**: Detects significant gaps in the time-series data during
    regular trading hours, which could indicate missing data.

Usage:
- You must specify the symbol, a date, and at least one bar type.
- Bar types are specified without the 'bars_' prefix (e.g., '5s', '1m').

Example Usage:
# Validate 5-second and 1-minute bars for TSLA for a specific date
python3 scripts/validate_data.py --symbol TSLA --date 2023-10-26 --bar-types 5s 1m

# Validate 5-second bars for multiple symbols on a specific date
python3 scripts/validate_data.py --symbols AAPL NVDA --date 2023-10-26 --bar-types 5s
"""
import os
import sys
import argparse
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import pandas as pd

# Add the project root to the Python path to allow for absolute imports
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from libs.db.clickhouse import ClickHouseDB
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)


class DataValidator:
    """
    Validates time-series data in ClickHouse.
    """

    def __init__(self):
        """Initializes the validator and connects to the database."""
        try:
            self.ch = ClickHouseDB()
            self.market_tz_str = config.get('ib.timezone', 'America/New_York')
            self.market_tz = ZoneInfo(self.market_tz_str)
            logger.info("DataValidator initialized successfully.")
        except Exception as e:
            logger.error("Failed to initialize DataValidator", error=e)
            sys.exit(1)

    @staticmethod
    def _parse_bar_type_to_timedelta(bar_type_str: str) -> timedelta:
        """
        Converts a bar type string (e.g., '5s', '1m', '1h') into a timedelta object.
        """
        unit = bar_type_str[-1].lower()
        value = int(bar_type_str[:-1])
        if unit == 's':
            return timedelta(seconds=value)
        elif unit == 'm':
            return timedelta(minutes=value)
        elif unit == 'h':
            return timedelta(hours=value)
        else:
            raise ValueError(f"Unsupported bar type unit: {unit}")

    def _get_trading_hours_utc(self, target_date: datetime.date) -> (datetime, datetime):
        """
        Calculates the start and end of regular trading hours in UTC for a given date.
        """
        # Regular trading hours in market timezone
        market_open = datetime.combine(target_date, time(9, 30), tzinfo=self.market_tz)
        market_close = datetime.combine(target_date, time(16, 0), tzinfo=self.market_tz)

        # Convert to UTC
        return market_open.astimezone(ZoneInfo("UTC")), market_close.astimezone(ZoneInfo("UTC"))

    def _fetch_bar_data(self, symbol: str, table_name: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
        """
        Fetches bar data for a given symbol and time range from ClickHouse.
        """
        query = f"""
        SELECT ts_start, open, high, low, close, volume
        FROM {table_name}
        WHERE symbol = %(symbol)s
          AND ts_start >= %(start_utc)s
          AND ts_start <= %(end_utc)s
        ORDER BY ts_start
        """
        params = {'symbol': symbol, 'start_utc': start_utc, 'end_utc': end_utc}
        
        try:
            results = self.ch.query(query, params)
            if not results:
                return pd.DataFrame()
            
            df = pd.DataFrame(results)
            df['ts_start'] = pd.to_datetime(df['ts_start'], utc=True)
            return df

        except Exception as e:
            logger.error(f"Failed to query table '{table_name}'", error=e)
            return pd.DataFrame()

    def run_validation(self, symbols: list, date_str: str, bar_types: list):
        """
        Runs the full validation suite for the given parameters.
        """
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.error("Invalid date format. Please use YYYY-MM-DD.")
            return

        for symbol in symbols:
            logger.info(f"========== Validating Symbol: {symbol.upper()} for {date_str} ==========")
            for bar_type in bar_types:
                table_name = f"bars_{bar_type}"
                try:
                    expected_interval = self._parse_bar_type_to_timedelta(bar_type)
                    self._validate_bar_data(symbol.upper(), target_date, table_name, expected_interval)
                except ValueError as e:
                    logger.error(f"Cannot validate bar type '{bar_type}'", error=e)
                except Exception as e:
                    logger.error(f"An unexpected error occurred during validation for {table_name}", error=e)
            logger.info(f"========== Finished validation for {symbol.upper()} ==========\n")

    def _validate_bar_data(self, symbol: str, target_date: datetime.date, table_name: str, expected_interval: timedelta):
        """
        Performs all validation checks for a specific symbol, date, and table.
        """
        logger.info(f"--- Checking table: {table_name} ---")

        start_utc, end_utc = self._get_trading_hours_utc(target_date)
        df = self._fetch_bar_data(symbol, table_name, start_utc, end_utc)

        # 1. Data Presence Check
        if df.empty:
            logger.warning(f" [FAIL] No data found for {symbol} in {table_name} between {start_utc} and {end_utc}.")
            return

        logger.info(f" [PASS] Found {len(df)} records for {symbol} in {table_name}.")

        # 2. Duplicate Timestamps Check
        duplicates = df[df.duplicated(subset=['ts_start'], keep=False)]
        if not duplicates.empty:
            logger.warning(f" [FAIL] Found {len(duplicates)} duplicate timestamps.")
            logger.warning(f"\n{duplicates.to_string()}")
        else:
            logger.info(" [PASS] No duplicate timestamps found.")
            
        # 3. OHLC Integrity Check
        invalid_ohlc = df[
            (df['high'] < df['open']) | (df['high'] < df['low']) | (df['high'] < df['close']) |
            (df['low'] > df['open']) | (df['low'] > df['high']) | (df['low'] > df['close'])
        ]
        if not invalid_ohlc.empty:
            logger.warning(f" [FAIL] Found {len(invalid_ohlc)} records with invalid OHLC values.")
            logger.warning(f"\n{invalid_ohlc.to_string()}")
        else:
            logger.info(" [PASS] All records have valid OHLC integrity.")

        # 4. Timestamp Gap Check
        df['time_diff'] = df['ts_start'].diff()
        # Gap threshold: we flag any gap that is more than 5x the expected interval.
        gap_threshold = expected_interval * 5
        gaps = df[df['time_diff'] > gap_threshold]
        
        if not gaps.empty:
            logger.warning(f" [FAIL] Found {len(gaps)} significant timestamp gaps (threshold: >{gap_threshold}).")
            for _, row in gaps.iterrows():
                prev_ts = row['ts_start'] - row['time_diff']
                logger.warning(f"  - Gap of {row['time_diff']} detected between "
                             f"{prev_ts.strftime('%H:%M:%S')} and {row['ts_start'].strftime('%H:%M:%S')}")
        else:
            logger.info(f" [PASS] No significant timestamp gaps found.")


def main():
    """Main execution function to parse arguments and run the validator."""
    parser = argparse.ArgumentParser(description="Validate time-series data in ClickHouse.")
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="Space-separated list of stock symbols to validate (e.g., TSLA AAPL).",
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="The date to validate data for, in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--bar-types",
        nargs="+",
        required=True,
        help="Space-separated list of bar types to validate (e.g., 5s 1m 1h).",
    )
    args = parser.parse_args()

    validator = DataValidator()
    validator.run_validation(args.symbols, args.date, args.bar_types)


if __name__ == "__main__":
    main()
# ```

# ### How to Use the Script

# 1.  **Save the File**: Make sure the code above is saved as `validate_data.py` inside your `scripts/` directory.

# 2.  **Run from the Command Line**: Open your terminal and run the script with the desired symbols, date, and bar types.

#     **Example 1: Check 5-second and 1-minute data for TSLA**
#     ```bash
#     python3 scripts/validate_data.py --symbols TSLA --date 2023-10-27 --bar-types 5s 1m
#     ```

#     **Example 2: Check 5-second data for multiple symbols**
#     ```bash
#     python3 scripts/validate_data.py --symbols NVDA AMD --date 2023-10-26 --bar-types 5s
    
