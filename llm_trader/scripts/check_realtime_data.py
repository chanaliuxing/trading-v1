#!/usr/bin/env python3
"""Check if Redis has realtime 1s bars data"""

import asyncio
import redis.asyncio as aioredis
import json
from datetime import datetime, timezone


async def main():
    print("="*100)
    print("CHECKING REALTIME DATA AVAILABILITY")
    print("="*100)
    print()

    # Connect to Redis
    try:
        redis_client = await aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
        print("✅ Connected to Redis")
    except Exception as e:
        print(f"❌ Failed to connect to Redis: {e}")
        return

    # Test symbols
    symbols = ["TSLA", "AAPL", "NVDA", "SPY", "QQQ"]

    print("\n" + "="*100)
    print("CHECKING SFS 1s BARS IN REDIS")
    print("="*100)

    for symbol in symbols:
        key = f"sfs:1s:{symbol}"

        try:
            # Get latest bar
            latest = await redis_client.lindex(key, 0)

            if latest:
                bar = json.loads(latest)
                ts = bar.get('ts', 0)

                # Convert timestamp to datetime
                if ts > 1000000000000:  # milliseconds
                    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                else:  # seconds
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)

                # Check how old the data is
                now = datetime.now(timezone.utc)
                age_seconds = (now - dt).total_seconds()
                age_minutes = age_seconds / 60

                print(f"\n{symbol}:")
                print(f"  ✅ Has data in Redis")
                print(f"  Latest timestamp: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                print(f"  Age: {age_minutes:.1f} minutes ago")

                if age_minutes < 5:
                    print(f"  🟢 REALTIME - Fresh data!")
                elif age_minutes < 60:
                    print(f"  🟡 Recent - May be realtime if market is open")
                else:
                    print(f"  🔴 OLD - Not realtime")

                # Show bar content
                print(f"  Content: close=${bar.get('close', 0):.2f}, volume={bar.get('volume', 0):.0f}")

                # Check how many bars available
                length = await redis_client.llen(key)
                print(f"  Total bars in buffer: {length}")

            else:
                print(f"\n{symbol}:")
                print(f"  ❌ No data in Redis")

        except Exception as e:
            print(f"\n{symbol}:")
            print(f"  ❌ Error: {e}")

    # Check if SFS-1s service is running
    print("\n" + "="*100)
    print("CHECKING SFS-1s SERVICE STATUS")
    print("="*100)

    # Try to detect service activity by checking TTL
    for symbol in ["TSLA", "AAPL"]:
        key = f"sfs:1s:{symbol}"
        try:
            ttl = await redis_client.ttl(key)
            if ttl > 0:
                print(f"\n{symbol}: TTL = {ttl}s ({ttl/60:.1f} minutes)")
                print(f"  Service likely running (TTL is set)")
            elif ttl == -1:
                print(f"\n{symbol}: No TTL (data won't expire)")
            else:
                print(f"\n{symbol}: Key doesn't exist or expired")
        except Exception as e:
            print(f"{symbol}: Error checking TTL: {e}")

    await redis_client.close()

    print("\n" + "="*100)
    print("SUMMARY")
    print("="*100)
    print()
    print("If you see fresh data (< 5 minutes old):")
    print("  ✅ SFS-1s service is running")
    print("  ✅ Can use Redis for realtime 1s bars")
    print()
    print("If data is old (> 1 hour):")
    print("  ❌ SFS-1s service may not be running")
    print("  ❌ Need to start: services/market/sfs_1s_service.py")
    print()
    print("If no data at all:")
    print("  ❌ SFS-1s service definitely not running")
    print("  ❌ Start service first before trading")
    print()


if __name__ == "__main__":
    asyncio.run(main())
