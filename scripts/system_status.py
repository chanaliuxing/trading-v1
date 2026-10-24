#!/usr/bin/env python3
"""
System Status Check
Comprehensive check of data flow and strategy status
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.db.ch_pool import get_client
from libs.logger import get_logger

logger = get_logger(__name__)

def main():
    db = get_client()  # Use global connection pool

    print("\n" + "="*80)
    print("📊 TRADING SYSTEM STATUS CHECK")
    print("="*80)

    # 1. Market Status
    now_et = datetime.now().astimezone()
    hour_et = now_et.hour
    dow = now_et.weekday()
    is_market_open = (dow < 5 and 9 <= hour_et < 16)

    print(f"\n⏰ Current Time: {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"   Market Status: {'🟢 OPEN' if is_market_open else '🔴 CLOSED'}")

    # 2. Data Flow Check
    print("\n📈 DATA FLOW STATUS:")
    sql = """
    SELECT
        symbol,
        COUNT(*) as bar_count,
        MAX(ts_start) as last_bar,
        dateDiff('second', MAX(ts_start), now()) as seconds_ago
    FROM trading.bars_5s
    WHERE ts_start >= today()
    GROUP BY symbol
    ORDER BY last_bar DESC
    LIMIT 5
    """
    recent_bars = db.query(sql)

    if recent_bars:
        print(f"   ✅ Data flowing - {len(recent_bars)} symbols with data today")
        for row in recent_bars[:3]:
            print(f"      {row['symbol']}: {row['bar_count']} bars, last bar {row['seconds_ago']}s ago")
    else:
        print("   ❌ NO DATA today - data ingestion may be stopped!")

    # 3. Features Status
    print("\n🔧 FEATURES STATUS:")
    sql = """
    SELECT
        symbol,
        COUNT(*) as feat_count,
        MAX(ts) as last_feature,
        dateDiff('second', MAX(ts), now()) as seconds_ago
    FROM trading.features_5s
    WHERE date(ts) = today()
    GROUP BY symbol
    ORDER BY last_feature DESC
    LIMIT 3
    """
    recent_features = db.query(sql)

    if recent_features:
        print(f"   ✅ Features computed - {len(recent_features)} symbols")
        for row in recent_features[:3]:
            print(f"      {row['symbol']}: {row['feat_count']} features, last {row['seconds_ago']}s ago")
    else:
        print("   ⚠️  NO FEATURES today - feature calculator may be stopped!")

    # 4. Strategy Status
    print("\n⚙️  STRATEGY STATUS:")

    # Check for strategy configs
    strategy_dir = Path(__file__).parent.parent / "strategies" / "configs"
    json_configs = list(strategy_dir.glob("*.json"))
    print(f"   📝 Strategy configs found: {len(json_configs)}")
    for config in json_configs[:5]:
        print(f"      - {config.stem}")

    # Check for recent exec events
    sql = """
    SELECT
        COUNT(*) as event_count,
        MAX(ts) as last_event
    FROM trading.exec_events
    WHERE date(ts) >= today()
    """
    exec_events = db.query(sql)

    if exec_events and exec_events[0]['event_count'] > 0:
        print(f"   ✅ Execution events today: {exec_events[0]['event_count']}")
        print(f"      Last event: {exec_events[0]['last_event']}")
    else:
        print("   ❌ NO EXECUTION EVENTS today")
        print("      Strategies may not be running!")

    # 5. Recommendations
    print("\n💡 RECOMMENDATIONS:")

    if not recent_bars:
        print("   🔴 CRITICAL: Start data ingestion services")
        print("      - Check if bars_5s service is running")
        print("      - Check IBKR connection")

    if not recent_features:
        print("   ⚠️  WARNING: Start feature calculator")
        print("      - Run feature calculator service")

    if not exec_events or exec_events[0]['event_count'] == 0:
        print("   ⚠️  WARNING: No strategies are running")
        print("      - Start strategy services")
        print("      - Check strategy configs in strategies/configs/")

    if is_market_open and recent_bars and recent_bars[0]['seconds_ago'] > 60:
        print("   ⚠️  WARNING: Data is stale (>60s old)")
        print("      - Check if real-time data feed is working")

    # 6. Current Positions
    print("\n💼 CURRENT POSITIONS:")
    sql = """
    SELECT
        symbol,
        net_qty,
        avg_px,
        net_qty * avg_px as notional
    FROM (
        SELECT
            symbol,
            SUM(qty) as net_qty,
            SUM(qty * price) / SUM(qty) as avg_px
        FROM trading.fills_live
        GROUP BY symbol
    )
    WHERE abs(net_qty) > 0.01
    ORDER BY abs(notional) DESC
    """
    positions = db.query(sql)

    if positions:
        print(f"   {len(positions)} open positions:")
        for pos in positions[:10]:
            side = "LONG" if pos['net_qty'] > 0 else "SHORT"
            print(f"      {pos['symbol']}: {side} {abs(pos['net_qty']):.2f} @ ${pos['avg_px']:.2f} (${pos['notional']:.2f})")
    else:
        print("   No open positions")

    print("\n" + "="*80)
    print("Status check complete!")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()
