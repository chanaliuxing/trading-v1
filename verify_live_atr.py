import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.append(os.getcwd())
from libs.db.ch_pool import get_client
from libs.strategy.components.indicator_lib import IndicatorLib

ET = ZoneInfo("America/New_York")

class DummyBar:
    def __init__(self, h, l, c):
        self.high, self.low, self.close = h, l, c

def verify_real_atr():
    client = get_client()
    now = datetime.now(ET)
    yesterday = now - timedelta(days=2)
    
    # 尝试不带 core 前缀
    query = f"""
        SELECT 
            toStartOfFiveMinutes(ts) as bar_start,
            argMin(price, ts) as open,
            max(price) as high,
            min(price) as low,
            argMax(price, ts) as close
        FROM lake_trades
        WHERE symbol = 'TQQQ'
          AND ts >= '{yesterday.strftime('%Y-%m-%d %H:%M:%S')}'
        GROUP BY bar_start
        ORDER BY bar_start DESC
        LIMIT 30
    """
    try:
        res = client.query(query).result_rows
    except Exception as e:
        print(f"Query failed: {e}")
        return

    if not res:
        print("No data found for TQQQ in lake_trades.")
        return

    bars = [DummyBar(r[2], r[3], r[4]) for r in reversed(res)]
    atr_val = IndicatorLib.atr(bars, 14)
    
    print(f"--- TQQQ ATR VERIFICATION ---")
    print(f"Sample size: {len(bars)} bars")
    print(f"Calculated ATR(14) from CH: {atr_val}")
    print(f"Seed ATR from JSON: 0.2358")

if __name__ == "__main__":
    verify_real_atr()
