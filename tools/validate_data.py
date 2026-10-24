import argparse
import os
from datetime import datetime, timedelta, UTC
import pandas as pd
import clickhouse_connect

# --- Configuration ---
# Load ClickHouse connection details from environment variables
CH_HOST = os.getenv("CH_HOST", "localhost")
CH_PORT = int(os.getenv("CH_PORT", 8123))
CH_DB = os.getenv("CH_DB", "trading")
CH_USER = os.getenv("CH_USER", "default")
CH_PASSWORD = os.getenv("CH_PASSWORD", "")

# Trading hours for gap analysis
RTH_START_TIME = "09:30:00"
RTH_END_TIME = "16:00:00"
EXPECTED_1M_BARS = 391 # 390 minutes from 9:30 to 16:00 inclusive
CH_TZ = "America/New_York"

def get_clickhouse_client():
    """Establishes and returns a ClickHouse client connection using clickhouse-connect."""
    try:
        client = clickhouse_connect.get_client(
            host=CH_HOST,
            port=CH_PORT,
            database=CH_DB,
            user=CH_USER,
            password=CH_PASSWORD,
        )
        client.command("SELECT 1")
        print(f"✅ Successfully connected to ClickHouse at {CH_HOST}:{CH_PORT} via HTTP")
        return client
    except Exception as e:
        print(f"🔥 Failed to connect to ClickHouse: {e}")
        return None

def check_data_gaps(client, symbol, date_str, table="bars_1m_ibkr"):
    """
    Checks for missing bars during regular trading hours (RTH).
    """
    print(f"\n🔎 [Gap Analysis] Checking for {symbol} in {table} on {date_str}...")
    
    start_datetime_str = f"{date_str} {RTH_START_TIME}"
    end_datetime_str = f"{date_str} {RTH_END_TIME}"

    query = f"""
    SELECT count()
    FROM {table}
    WHERE symbol = %(symbol)s
      AND ts_start >= toDateTime(%(start_dt)s, %(tz)s)
      AND ts_start <= toDateTime(%(end_dt)s, %(tz)s)
    """
    
    try:
        result = client.query(query, parameters={
            'symbol': symbol, 
            'start_dt': start_datetime_str, 
            'end_dt': end_datetime_str,
            'tz': CH_TZ
        })
        bar_count = result.result_rows[0][0] if result.result_rows else 0
        
        if bar_count >= EXPECTED_1M_BARS:
            print(f"  ✅ OK: Found {bar_count} bars. Looks complete.")
            return True
        elif 0 < bar_count < EXPECTED_1M_BARS:
            print(f"  ⚠️ WARN: Found only {bar_count}/{EXPECTED_1M_BARS} bars. Data is incomplete.")
            return False
        else:
            print(f"  🔥 FAIL: Found 0 bars for symbol {symbol} on this date.")
            return False
            
    except Exception as e:
        if "DB::Exception: Table" in str(e) and "doesn't exist" in str(e):
            print(f"  🤷 INFO: Table {table} does not exist. Skipping check.")
        else:
            print(f"  🔥 ERROR: Query failed for gap analysis. {e}")
        return False

def check_null_values(client, symbol, date_str, table="bars_1m_ibkr"):
    """
    Checks for NULL values in critical OHLCV columns.
    """
    print(f"\n🔎 [Null Value Check] Checking for {symbol} in {table} on {date_str}...")
    
    columns_to_check = ["open", "high", "low", "close", "volume"]
    has_nulls = False
    
    for col in columns_to_check:
        query = f"""
        SELECT count()
        FROM {table}
        WHERE symbol = %(symbol)s
          AND toDate(ts_start) = toDate(%(date)s)
          AND ({col} IS NULL OR isNaN({col}))
        """
        try:
            result = client.query(query, parameters={'symbol': symbol, 'date': date_str})
            null_count = result.result_rows[0][0] if result.result_rows else 0
            if null_count > 0:
                print(f"  🔥 FAIL: Found {null_count} NULL or NaN values in column '{col}'.")
                has_nulls = True
            else:
                print(f"  ✅ OK: Column '{col}' has no NULLs.")
        except Exception as e:
            if "DB::Exception: Missing column" in str(e):
                print(f"  🤷 INFO: Column '{col}' not in table {table}. Skipping.")
            else:
                print(f"  🔥 ERROR: Query failed for column '{col}'. {e}")
                has_nulls = True
                break # Stop on first error
    
    return not has_nulls

def check_outliers(client, symbol, date_str, table="bars_1m_ibkr", std_dev_threshold=7):
    """
    Performs a basic outlier check on price and volume using standard deviation.
    """
    print(f"\n🔎 [Outlier Check] Checking for {symbol} in {table} on {date_str}...")
    
    query = f"""
    SELECT
        avg(close) AS avg_close,
        stddevPop(close) AS std_close,
        avg(volume) AS avg_vol,
        stddevPop(volume) AS std_vol
    FROM {table}
    WHERE symbol = %(symbol)s AND toDate(ts_start) = toDate(%(date)s)
    """
    
    try:
        stats_result = client.query(query, parameters={'symbol': symbol, 'date': date_str})
        if not stats_result.result_rows or stats_result.result_rows[0][0] is None:
            print("  🤷 INFO: Not enough data to calculate stats. Skipping outlier check.")
            return True

        avg_close, std_close, avg_vol, std_vol = stats_result.result_rows[0]
        
        # Define outlier boundaries
        price_upper_bound = avg_close + std_dev_threshold * std_close
        price_lower_bound = avg_close - std_dev_threshold * std_close
        volume_upper_bound = avg_vol + std_dev_threshold * std_vol

        outlier_query = f"""
        SELECT count()
        FROM {table}
        WHERE symbol = %(symbol)s AND toDate(ts_start) = toDate(%(date)s)
          AND (close > %(p_upper)s OR close < %(p_lower)s OR volume > %(v_upper)s)
        """
        
        outlier_result = client.query(outlier_query, parameters={
            'symbol': symbol, 'date': date_str,
            'p_upper': price_upper_bound, 'p_lower': price_lower_bound, 'v_upper': volume_upper_bound
        })
        outlier_count = outlier_result.result_rows[0][0]
        
        if outlier_count > 0:
            print(f"  ⚠️ WARN: Found {outlier_count} potential outliers (price/volume > {std_dev_threshold} std dev).")
            return False
        else:
            print(f"  ✅ OK: No significant outliers detected.")
            return True

    except Exception as e:
        print(f"  🔥 ERROR: Query failed for outlier analysis. {e}")
        return False

def check_freshness(client, symbol, table="bars_1m_ibkr", minutes_stale=15):
    """
    Checks if the latest data point for a symbol is recent.
    """
    print(f"\n🔎 [Freshness Check] Checking latest data for {symbol} in {table}...")
    
    query = f"""
    SELECT max(ts_start)
    FROM {table}
    WHERE symbol = %(symbol)s
    """
    
    try:
        result = client.query(query, parameters={'symbol': symbol})
        latest_ts = result.result_rows[0][0] if result.result_rows else None
        
        if latest_ts is None:
            print(f"  🔥 FAIL: No data found for symbol {symbol}.")
            return False

        now_utc = datetime.now(UTC)
        if not latest_ts.tzinfo:
            latest_ts = latest_ts.replace(tzinfo=now_utc.tzinfo)

        time_diff = now_utc - latest_ts
        
        if time_diff > timedelta(minutes=minutes_stale):
            print(f"  ⚠️ WARN: Data is stale. Last entry was at {latest_ts} ({time_diff.seconds // 60} minutes ago).")
            return False
        else:
            print(f"  ✅ OK: Data is fresh. Last entry was at {latest_ts}.")
            return True
            
    except Exception as e:
        print(f"  🔥 ERROR: Query failed for freshness check. {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="ClickHouse Data Validation Tool.")
    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Date to run checks for (format: YYYY-MM-DD). Defaults to today."
    )
    parser.add_argument(
        "--symbols",
        type=str,
        required=True,
        help="Comma-separated list of symbols to check (e.g., 'AAPL,TSLA,NVDA')."
    )
    parser.add_argument(
        "--checks",
        type=str,
        default="gaps,nulls,outliers,freshness",
        help="Comma-separated list of checks to run (e.g., 'gaps,nulls')."
    )
    parser.add_argument(
        "--table",
        type=str,
        default="bars_1m_ibkr",
        help="The table to perform checks on. Defaults to 'bars_1m_ibkr'."
    )
    
    args = parser.parse_args()
    symbols_to_check = [s.strip().upper() for s in args.symbols.split(',')]
    checks_to_run = [c.strip().lower() for c in args.checks.split(',')]
    
    print(f"--- Starting Data Validation for {args.date} ---")
    print(f"Symbols: {symbols_to_check}")
    print(f"Table: {args.table}")
    print(f"Checks: {checks_to_run}")
    
    client = get_clickhouse_client()
    if not client:
        return

    summary = {}

    for symbol in symbols_to_check:
        print(f"\n{'='*20} Validating Symbol: {symbol} {'='*20}")
        results = {}
        if "gaps" in checks_to_run:
            results["gaps"] = check_data_gaps(client, symbol, args.date, args.table)
        if "nulls" in checks_to_run:
            results["nulls"] = check_null_values(client, symbol, args.date, args.table)
        if "outliers" in checks_to_run:
            results["outliers"] = check_outliers(client, symbol, args.date, args.table)
        if "freshness" in checks_to_run:
            results["freshness"] = check_freshness(client, symbol, args.table)
        summary[symbol] = all(results.values())

    print(f"\n{'='*20} Validation Summary {'='*20}")
    all_passed = True
    for symbol, passed in summary.items():
        status = "✅ PASSED" if passed else "🔥 FAILED"
        print(f"- {symbol}: {status}")
        if not passed:
            all_passed = False
    
    print("\n--- Validation Complete ---")
    if not all_passed:
        print("\nSome checks failed. Please review the logs above.")
        exit(1)
    else:
        print("\nAll checks passed successfully.")

if __name__ == "__main__":
    main()