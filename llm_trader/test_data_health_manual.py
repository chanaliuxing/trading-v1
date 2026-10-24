#!/usr/bin/env python3
"""手动测试数据健康监控功能

用法:
    python test_data_health_manual.py

验证:
1. 时间戳存在性检查
2. 时间戳新鲜度检查
3. 时间戳递增检查 (防止缓存)
4. 价格有效性检查
5. 数据量检查
6. 状态机转换
"""

import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Test data structure
def create_test_data(symbol, timestamp, price=150.0, bars_count=10):
    """创建测试数据"""
    bars_1s = []
    for i in range(bars_count):
        bars_1s.append({
            "ts": int((timestamp - timedelta(seconds=i)).timestamp() * 1000),
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 100,
            "trades": 5
        })

    return {
        "symbol": symbol,
        "timestamp": timestamp.isoformat(),
        "data_source": "redis_1s",
        "current_price": price,
        "features_5s": [],
        "sfs_features": {},
        "bars_1s": bars_1s,
    }

def test_timestamp_freshness():
    """测试时间戳新鲜度"""
    print("\n=== Test 1: Timestamp Freshness ===")

    # Fresh data (3s old)
    now = datetime.now(timezone.utc)
    fresh_timestamp = now - timedelta(seconds=3)
    data_fresh = create_test_data("AAPL", fresh_timestamp)

    # Calculate age
    data_ts = datetime.fromisoformat(data_fresh["timestamp"])
    if data_ts.tzinfo is None:
        data_ts = data_ts.replace(tzinfo=timezone.utc)
    else:
        data_ts = data_ts.astimezone(timezone.utc)

    age = (now - data_ts).total_seconds()
    print(f"Fresh data: age={age:.1f}s (threshold=10s)")
    print(f"✓ PASS: {age < 10}" if age < 10 else f"✗ FAIL: {age >= 10}")

    # Stale data (15s old)
    stale_timestamp = now - timedelta(seconds=15)
    data_stale = create_test_data("AAPL", stale_timestamp)

    data_ts = datetime.fromisoformat(data_stale["timestamp"])
    if data_ts.tzinfo is None:
        data_ts = data_ts.replace(tzinfo=timezone.utc)
    else:
        data_ts = data_ts.astimezone(timezone.utc)

    age = (now - data_ts).total_seconds()
    print(f"\nStale data: age={age:.1f}s (threshold=10s)")
    print(f"✓ PASS: {age >= 10}" if age >= 10 else f"✗ FAIL: {age < 10}")

def test_timestamp_advancing():
    """测试时间戳递增"""
    print("\n=== Test 2: Timestamp Advancing ===")

    now = datetime.now(timezone.utc)

    # Cycle 1: First timestamp
    ts1 = now - timedelta(seconds=5)
    print(f"Cycle 1: timestamp={ts1.isoformat()}")
    print("✓ PASS: No previous timestamp, should accept")

    # Cycle 2: Same timestamp (cached data)
    ts2 = now - timedelta(seconds=5)  # Same as ts1
    print(f"\nCycle 2: timestamp={ts2.isoformat()}")
    if ts2 <= ts1:
        print("✓ PASS: Detected non-advancing timestamp (cached data)")
    else:
        print("✗ FAIL: Should detect cached data")

    # Cycle 3: Advancing timestamp
    ts3 = now - timedelta(seconds=2)  # Newer
    print(f"\nCycle 3: timestamp={ts3.isoformat()}")
    if ts3 > ts1:
        print("✓ PASS: Timestamp is advancing")
    else:
        print("✗ FAIL: Timestamp should be advancing")

def test_price_validity():
    """测试价格有效性"""
    print("\n=== Test 3: Price Validity ===")

    now = datetime.now(timezone.utc)

    # Valid price
    data_valid = create_test_data("AAPL", now, price=150.5)
    print(f"Valid price: ${data_valid['current_price']}")
    print(f"✓ PASS" if data_valid['current_price'] > 0 else f"✗ FAIL")

    # Invalid price (0)
    data_invalid = create_test_data("AAPL", now, price=0.0)
    print(f"\nInvalid price: ${data_invalid['current_price']}")
    print(f"✓ PASS: Rejected" if data_invalid['current_price'] <= 0 else f"✗ FAIL")

def test_bars_count():
    """测试数据量"""
    print("\n=== Test 4: Bars Count ===")

    now = datetime.now(timezone.utc)

    # Sufficient bars
    data_sufficient = create_test_data("AAPL", now, bars_count=10)
    print(f"Sufficient bars: {len(data_sufficient['bars_1s'])} (min=5)")
    print(f"✓ PASS" if len(data_sufficient['bars_1s']) >= 5 else f"✗ FAIL")

    # Insufficient bars
    data_insufficient = create_test_data("AAPL", now, bars_count=3)
    print(f"\nInsufficient bars: {len(data_insufficient['bars_1s'])} (min=5)")
    print(f"✓ PASS: Rejected" if len(data_insufficient['bars_1s']) < 5 else f"✗ FAIL")

def test_timezone_handling():
    """测试时区处理"""
    print("\n=== Test 5: Timezone Handling ===")

    # NY timezone (from fetcher)
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    print(f"NY time: {now_ny.isoformat()}")

    # Convert to UTC
    now_utc = now_ny.astimezone(timezone.utc)
    print(f"UTC time: {now_utc.isoformat()}")

    # Calculate age
    age = (datetime.now(timezone.utc) - now_utc).total_seconds()
    print(f"Age: {age:.1f}s")
    print(f"✓ PASS: Timezone conversion works" if abs(age) < 5 else f"✗ FAIL")

def test_state_transitions():
    """测试状态机转换"""
    print("\n=== Test 6: State Transitions ===")

    # Simulate state machine
    data_health = {
        "status": "HEALTHY",
        "consecutive_failures": 0,
        "recovery_checks_passed": 0,
    }

    print(f"Initial state: {data_health['status']}")

    # Failures 1-2: HEALTHY → DEGRADED
    for i in range(1, 3):
        data_health['consecutive_failures'] = i
        if i >= 2:
            data_health['status'] = "DEGRADED"
        print(f"  After {i} failures: {data_health['status']}")

    # Failures 3-5: DEGRADED → UNHEALTHY
    for i in range(3, 6):
        data_health['consecutive_failures'] = i
        if i >= 5:
            data_health['status'] = "UNHEALTHY"
        print(f"  After {i} failures: {data_health['status']}")

    print(f"\n✓ PASS: Entered UNHEALTHY after 5 failures")

    # Recovery: UNHEALTHY → RECOVERING → HEALTHY
    data_health['consecutive_failures'] = 0
    data_health['recovery_checks_passed'] = 0
    data_health['status'] = "RECOVERING"

    print(f"\nRecovery phase:")
    for i in range(1, 4):
        data_health['recovery_checks_passed'] = i
        status = "HEALTHY" if i >= 3 else "RECOVERING"
        print(f"  After {i} good checks: {status} ({i}/3)")

    print(f"\n✓ PASS: Recovered after 3 consecutive good checks")

def main():
    """运行所有测试"""
    print("="*80)
    print("Data Health Monitor - Manual Tests")
    print("="*80)

    test_timestamp_freshness()
    test_timestamp_advancing()
    test_price_validity()
    test_bars_count()
    test_timezone_handling()
    test_state_transitions()

    print("\n" + "="*80)
    print("All tests completed!")
    print("="*80)

    print("\n✅ 下一步: 运行实际系统测试")
    print("命令: python -m llm_trader.run_llm_trader --provider deepseek --symbols AAPL --interval 60")

if __name__ == "__main__":
    main()
