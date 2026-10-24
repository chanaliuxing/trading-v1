import sys
from pathlib import Path
from datetime import datetime, timezone
import structlog
import time # Added import

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from libs.db.ch_pool import get_client

logger = structlog.get_logger(__name__)

def query_max_ts(table_name: str, symbol: str) -> datetime | None:
    client = get_client()
    query = f"""
        SELECT max(ts) as latest_ts
        FROM {table_name}
        WHERE symbol = %(symbol)s
        -- cache_bust={int(time.time())}
    """
    try:
        result = client.query(query, {"symbol": symbol}) # settings passed through wrapper
        if result and result[0]['latest_ts']:
            return result[0]['latest_ts']
    except Exception as e:
        logger.error("query_max_ts_failed", table=table_name, symbol=symbol, error=str(e))
    return None

def main():
    symbols = ["TSLA", "NVDA"]
    tables = ["trading.features_1m_active", "trading.features_1m_with_labels"]

    now_utc = datetime.now(timezone.utc)
    print(f"Current UTC time: {now_utc} (tzinfo: {now_utc.tzinfo})")

    for table in tables:
        for symbol in symbols:
            latest_ts = query_max_ts(table, symbol)
            if latest_ts:
                # Ensure latest_ts is timezone-aware for correct comparison
                # ClickHouse DateTime often returns naive, assume America/New_York as per project context
                if latest_ts.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    local_tz = ZoneInfo("America/New_York")
                    latest_ts = latest_ts.replace(tzinfo=local_tz)
                    # Convert to UTC for consistent comparison
                    latest_ts_utc = latest_ts.astimezone(timezone.utc)
                else:
                    latest_ts_utc = latest_ts.astimezone(timezone.utc)

                age_hours = (now_utc - latest_ts_utc).total_seconds() / 3600
                print(f"Table: {table}, Symbol: {symbol}")
                print(f"  Latest TS (original): {latest_ts} (tzinfo: {latest_ts.tzinfo})")
                print(f"  Latest TS (UTC):      {latest_ts_utc} (tzinfo: {latest_ts_utc.tzinfo})")
                print(f"  Age in hours: {age_hours:.2f}")
            else:
                print(f"Table: {table}, Symbol: {symbol} - No latest timestamp found.")

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    main()
