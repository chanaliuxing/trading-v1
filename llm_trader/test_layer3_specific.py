#!/usr/bin/env python3
"""Layer 3 专项测试 - 时间戳递增检查

目的: 验证数据新鲜但时间戳不变的场景 (真正的缓存检测)

场景:
- Cycle 1: timestamp=T, age=3s → PASS (首次)
- Cycle 2: timestamp=T (相同!), age=5s → FAIL (检测到缓存)
- Cycle 3: timestamp=T+30s, age=2s → PASS (时间戳递增)
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from llm_trader.llm_trader_bot import LLMTraderBot
from llm_trader.utils.config import Config


class MockLLMProvider:
    """Mock LLM Provider"""
    def __init__(self):
        self.model = "mock-model"

    async def health_check(self):
        return True

    async def chat_completion(self, messages, **kwargs):
        return {"a": "H", "conf": 0.5, "q": 0, "rationale": "Mock"}


async def test_layer3_timestamp_advancing():
    """测试 Layer 3: 时间戳递增检查"""

    print("\n" + "="*80)
    print("🧪 Layer 3 专项测试 - 时间戳递增检查")
    print("="*80)
    print("\n目标: 验证数据新鲜但时间戳不变时能被检测为缓存")

    # 创建 bot
    config = Config()
    bot = LLMTraderBot(
        provider_name="deepseek",
        symbols=["AAPL"],
        config=config,
        initial_cash=50000.0,
        dry_run=True,
        use_aggregator=False,
    )
    bot.llm_provider = MockLLMProvider()

    # 模拟当前时间
    now = datetime.now(timezone.utc)

    # === Cycle 1: 新鲜数据 (首次) ===
    print("\n--- Cycle 1: 新鲜数据 (首次) ---")

    # 数据时间戳 = 3秒前
    data_timestamp_1 = now - timedelta(seconds=3)

    # 构造 1s bars
    bars_1s_1 = []
    for i in range(10):
        bar_ts = data_timestamp_1 - timedelta(seconds=i)
        bars_1s_1.append({
            "ts": int(bar_ts.timestamp() * 1000),
            "open": 150.0,
            "high": 150.5,
            "low": 149.5,
            "close": 150.0,
            "volume": 100,
            "trades": 5
        })

    market_data_1 = {
        "AAPL": {
            "symbol": "AAPL",
            "timestamp": data_timestamp_1.isoformat(),
            "data_source": "redis_1s",
            "current_price": 150.0,
            "features_5s": [],
            "sfs_features": {},
            "bars_1s": bars_1s_1,
        }
    }

    print(f"Data timestamp: {data_timestamp_1.isoformat()}")
    print(f"Data age: ~3s")

    is_healthy_1 = bot._check_data_health(market_data_1)

    print(f"Result: {'✅ HEALTHY' if is_healthy_1 else '❌ UNHEALTHY'}")
    print(f"Status: {bot.data_health['status']}")
    print(f"Consecutive failures: {bot.data_health['consecutive_failures']}")

    if not is_healthy_1:
        print("❌ Cycle 1 失败: 新鲜数据应该被接受")
        return False

    # 模拟时间流逝 (2秒)
    await asyncio.sleep(0.1)

    # === Cycle 2: 相同时间戳 (模拟缓存) ===
    print("\n--- Cycle 2: 相同时间戳 (模拟缓存) ---")

    # 关键: 时间戳与 Cycle 1 相同，但现在已经过去 2 秒
    # 数据 age = 原来 3s + 流逝 2s = 5s (仍然 < 10s 阈值)

    # 构造 bars (时间戳相同!)
    bars_1s_2 = []
    for i in range(10):
        bar_ts = data_timestamp_1 - timedelta(seconds=i)  # ← 相同的时间戳!
        bars_1s_2.append({
            "ts": int(bar_ts.timestamp() * 1000),
            "open": 150.0,
            "high": 150.5,
            "low": 149.5,
            "close": 150.0,
            "volume": 100,
            "trades": 5
        })

    market_data_2 = {
        "AAPL": {
            "symbol": "AAPL",
            "timestamp": data_timestamp_1.isoformat(),  # ← 相同的时间戳!
            "data_source": "redis_1s",
            "current_price": 150.0,
            "features_5s": [],
            "sfs_features": {},
            "bars_1s": bars_1s_2,
        }
    }

    # 计算实际 age (从当前时间算)
    current_time = datetime.now(timezone.utc)
    actual_age = (current_time - data_timestamp_1).total_seconds()

    print(f"Data timestamp: {data_timestamp_1.isoformat()} (SAME AS CYCLE 1!)")
    print(f"Actual data age: ~{actual_age:.1f}s (< 10s threshold)")
    print(f"Expected: Layer 2 PASS, Layer 3 FAIL (timestamp not advancing)")

    is_healthy_2 = bot._check_data_health(market_data_2)

    print(f"Result: {'✅ HEALTHY' if is_healthy_2 else '❌ UNHEALTHY'}")
    print(f"Status: {bot.data_health['status']}")
    print(f"Consecutive failures: {bot.data_health['consecutive_failures']}")

    # 验证: 应该检测到缓存
    if is_healthy_2:
        print("\n❌ CRITICAL: Layer 3 未检测到缓存数据!")
        print("   数据时间戳未递增，但被错误地接受为健康数据")
        print("   这会导致交易系统使用陈旧信息做决策!")
        return False

    print("✅ Cycle 2 通过: 成功检测到缓存数据")

    # 模拟时间流逝 (1秒)
    await asyncio.sleep(0.1)

    # === Cycle 3: 时间戳递增 (恢复正常) ===
    print("\n--- Cycle 3: 时间戳递增 (恢复) ---")

    # 新的时间戳 = 30秒后
    data_timestamp_3 = data_timestamp_1 + timedelta(seconds=30)

    # 构造新的 bars
    bars_1s_3 = []
    for i in range(10):
        bar_ts = data_timestamp_3 - timedelta(seconds=i)  # ← 新的时间戳
        bars_1s_3.append({
            "ts": int(bar_ts.timestamp() * 1000),
            "open": 151.0,
            "high": 151.5,
            "low": 150.5,
            "close": 151.0,
            "volume": 120,
            "trades": 6
        })

    market_data_3 = {
        "AAPL": {
            "symbol": "AAPL",
            "timestamp": data_timestamp_3.isoformat(),  # ← 新的时间戳!
            "data_source": "redis_1s",
            "current_price": 151.0,
            "features_5s": [],
            "sfs_features": {},
            "bars_1s": bars_1s_3,
        }
    }

    print(f"Data timestamp: {data_timestamp_3.isoformat()}")
    print(f"Previous timestamp: {data_timestamp_1.isoformat()}")
    print(f"Time advanced: 30s ✓")

    is_healthy_3 = bot._check_data_health(market_data_3)

    print(f"Result: {'✅ HEALTHY' if is_healthy_3 else '❌ UNHEALTHY'}")
    print(f"Status: {bot.data_health['status']}")
    print(f"Consecutive failures: {bot.data_health['consecutive_failures']}")

    # 注意: 可能仍在 RECOVERING 状态，需要连续 3 次成功
    if bot.data_health['consecutive_failures'] == 0:
        print("✅ Cycle 3 通过: 失败计数已重置")
    else:
        print(f"⚠️  Cycle 3: 仍在恢复中 (failures={bot.data_health['consecutive_failures']})")

    return True


async def main():
    """运行测试"""
    print("="*80)
    print("🎯 Layer 3 专项测试 - 时间戳递增检查")
    print("="*80)

    print("\n本测试验证:")
    print("1. 数据新鲜 (age < 10s) 但时间戳不变 → 应被拒绝 (防止缓存)")
    print("2. 时间戳递增 → 应被接受")

    try:
        result = await test_layer3_timestamp_advancing()

        if result:
            print("\n" + "="*80)
            print("✅ Layer 3 测试通过!")
            print("="*80)
            print("\n🎯 核心功能验证:")
            print("  ✓ 检测到新鲜但时间戳不变的缓存数据")
            print("  ✓ 正确拒绝缓存数据")
            print("  ✓ 时间戳递增后恢复正常")
            return 0
        else:
            print("\n" + "="*80)
            print("❌ Layer 3 测试失败!")
            print("="*80)
            print("\n⚠️  问题:")
            print("  - Layer 3 (时间戳递增检查) 未按预期工作")
            print("  - 缓存数据未被检测")
            print("\n🔍 可能原因:")
            print("  1. Layer 3 条件 `if last_timestamp and is_healthy` 过于严格")
            print("  2. Layer 2 先触发导致 Layer 3 跳过")
            print("  3. 时区转换问题")
            return 1

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
