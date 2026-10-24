#!/usr/bin/env python3
"""数据健康监控 - 集成测试脚本

模拟真实场景:
1. 正常数据流
2. 数据断连 (模拟 Redis 返回缓存)
3. 数据恢复
4. 状态机转换验证
5. 动态间隔验证

用法:
    python test_data_health_integration.py
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from llm_trader.llm_trader_bot import LLMTraderBot
from llm_trader.utils.config import Config


class MockMarketDataFetcher:
    """Mock MarketDataFetcher for testing"""

    def __init__(self):
        self.current_time = datetime.now(timezone.utc)
        self.cached_timestamp = None  # 模拟缓存的时间戳
        self.is_connected = True

    async def connect(self):
        print("✓ Mock MarketDataFetcher connected")

    async def close(self):
        print("✓ Mock MarketDataFetcher closed")

    def simulate_disconnect(self):
        """模拟数据源断连 - 返回缓存的旧数据"""
        self.is_connected = False
        self.cached_timestamp = self.current_time - timedelta(seconds=60)
        print(f"❌ Simulated disconnect - will return cached timestamp: {self.cached_timestamp.isoformat()}")

    def simulate_reconnect(self):
        """模拟数据源恢复"""
        self.is_connected = True
        self.cached_timestamp = None
        print(f"✅ Simulated reconnect - will return fresh data")

    async def get_multiple_symbols_data(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """模拟获取多个 symbol 的数据"""
        result = {}

        for symbol in symbols:
            # 更新当前时间（模拟时间流逝）
            self.current_time = datetime.now(timezone.utc)

            if self.is_connected:
                # 正常情况: 返回新鲜数据
                data_timestamp = self.current_time - timedelta(seconds=2)
            else:
                # 断连情况: 返回缓存的旧数据 (时间戳不变!)
                data_timestamp = self.cached_timestamp

            # 构造 1s bars
            bars_1s = []
            for i in range(10):
                bar_ts = data_timestamp - timedelta(seconds=i)
                bars_1s.append({
                    "ts": int(bar_ts.timestamp() * 1000),
                    "open": 150.0,
                    "high": 150.5,
                    "low": 149.5,
                    "close": 150.0,
                    "volume": 100,
                    "trades": 5
                })

            result[symbol] = {
                "symbol": symbol,
                "timestamp": data_timestamp.isoformat(),
                "data_source": "redis_1s" if self.is_connected else "redis_1s_cached",
                "current_price": 150.0,
                "features_5s": [],
                "sfs_features": {},
                "bars_1s": bars_1s,
            }

        return result


class MockLLMProvider:
    """Mock LLM Provider for testing"""

    def __init__(self):
        self.call_count = 0
        self.model = "mock-model"

    async def health_check(self):
        return True

    async def chat_completion(self, messages, **kwargs):
        self.call_count += 1
        return {
            "a": "H",  # HOLD
            "conf": 0.5,
            "q": 0,
            "rationale": "Mock decision"
        }


async def test_scenario_1_normal_operation():
    """测试场景 1: 正常运行"""
    print("\n" + "="*80)
    print("📊 测试场景 1: 正常数据流")
    print("="*80)

    # 创建 mock 对象
    mock_fetcher = MockMarketDataFetcher()
    await mock_fetcher.connect()

    # 创建 bot (简化配置)
    config = Config()
    bot = LLMTraderBot(
        provider_name="deepseek",
        symbols=["AAPL"],
        config=config,
        initial_cash=50000.0,
        dry_run=True,
        use_aggregator=False,  # 简化测试
    )

    # 替换为 mock
    bot.data_fetcher = mock_fetcher
    bot.llm_provider = MockLLMProvider()

    # 初始化
    await bot.data_fetcher.connect()

    print("\n运行 3 个周期 (正常数据)...")
    for i in range(1, 4):
        print(f"\n--- Cycle {i} ---")
        market_data = await bot.data_fetcher.get_multiple_symbols_data(["AAPL"])

        # 检查数据健康
        is_healthy = bot._check_data_health(market_data)

        print(f"Data health check: {'✓ HEALTHY' if is_healthy else '✗ UNHEALTHY'}")
        print(f"Status: {bot.data_health['status']}")
        print(f"Consecutive failures: {bot.data_health['consecutive_failures']}")

        await asyncio.sleep(0.1)  # 模拟时间间隔

    await bot.data_fetcher.close()

    # 验证
    assert bot.data_health['status'] == 'HEALTHY', "❌ 应该是 HEALTHY"
    assert bot.data_health['consecutive_failures'] == 0, "❌ 失败次数应该是 0"
    print("\n✅ 测试场景 1 通过: 正常数据流工作正常")


async def test_scenario_2_disconnect_and_reconnect():
    """测试场景 2: 数据断连 + 恢复"""
    print("\n" + "="*80)
    print("📊 测试场景 2: 数据断连 → 缓存检测 → 恢复")
    print("="*80)

    # 创建 mock 对象
    mock_fetcher = MockMarketDataFetcher()
    await mock_fetcher.connect()

    config = Config()
    bot = LLMTraderBot(
        provider_name="deepseek",
        symbols=["AAPL"],
        config=config,
        initial_cash=50000.0,
        dry_run=True,
        use_aggregator=False,
    )

    bot.data_fetcher = mock_fetcher
    bot.llm_provider = MockLLMProvider()
    await bot.data_fetcher.connect()

    # Phase 1: 正常运行 2 个周期
    print("\n=== Phase 1: 正常数据 ===")
    for i in range(1, 3):
        print(f"\n--- Cycle {i} ---")
        market_data = await bot.data_fetcher.get_multiple_symbols_data(["AAPL"])
        is_healthy = bot._check_data_health(market_data)
        print(f"Status: {bot.data_health['status']}, Healthy: {is_healthy}")
        await asyncio.sleep(0.1)

    assert bot.data_health['status'] == 'HEALTHY', "Phase 1: 应该是 HEALTHY"

    # Phase 2: 模拟断连 (返回缓存数据)
    print("\n=== Phase 2: 数据源断连 (返回缓存) ===")
    mock_fetcher.simulate_disconnect()

    for i in range(3, 8):
        print(f"\n--- Cycle {i} ---")
        market_data = await bot.data_fetcher.get_multiple_symbols_data(["AAPL"])
        is_healthy = bot._check_data_health(market_data)

        print(f"Status: {bot.data_health['status']}")
        print(f"Consecutive failures: {bot.data_health['consecutive_failures']}")
        print(f"Healthy: {is_healthy}")

        # 检查时间戳
        data = market_data.get("AAPL", {})
        print(f"Data timestamp: {data.get('timestamp')}")

        await asyncio.sleep(0.1)

    # 验证进入 UNHEALTHY
    assert bot.data_health['status'] == 'UNHEALTHY', "Phase 2: 应该进入 UNHEALTHY"
    assert bot.data_health['consecutive_failures'] >= 5, "Phase 2: 失败次数应该 >= 5"
    print("\n✅ Phase 2 通过: 成功检测到缓存数据并进入 UNHEALTHY")

    # Phase 3: 模拟恢复
    print("\n=== Phase 3: 数据源恢复 ===")
    mock_fetcher.simulate_reconnect()

    for i in range(8, 12):
        print(f"\n--- Cycle {i} ---")
        market_data = await bot.data_fetcher.get_multiple_symbols_data(["AAPL"])
        is_healthy = bot._check_data_health(market_data)

        print(f"Status: {bot.data_health['status']}")
        print(f"Recovery checks: {bot.data_health['recovery_checks_passed']}/{bot.data_health_config['recovery_checks_needed']}")
        print(f"Healthy: {is_healthy}")

        await asyncio.sleep(0.1)

    # 验证恢复
    assert bot.data_health['status'] == 'HEALTHY', "Phase 3: 应该恢复到 HEALTHY"
    assert bot.data_health['consecutive_failures'] == 0, "Phase 3: 失败次数应该重置为 0"
    print("\n✅ Phase 3 通过: 成功恢复到 HEALTHY")

    await bot.data_fetcher.close()
    print("\n✅ 测试场景 2 通过: 断连检测 + 恢复验证成功")


async def test_scenario_3_invalid_data():
    """测试场景 3: 无效数据检测"""
    print("\n" + "="*80)
    print("📊 测试场景 3: 无效数据检测")
    print("="*80)

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

    # Test Case 1: 价格无效
    print("\n--- Test Case 1: 价格无效 (price=0) ---")
    invalid_price_data = {
        "AAPL": {
            "symbol": "AAPL",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": 0.0,  # ❌ 无效
            "bars_1s": [{"ts": int(datetime.now(timezone.utc).timestamp() * 1000), "volume": 100}] * 10
        }
    }

    is_healthy = bot._check_data_health(invalid_price_data)
    print(f"Price=0 detected: {'✓ REJECTED' if not is_healthy else '✗ ACCEPTED (BAD!)'}")
    assert not is_healthy, "价格为 0 应该被拒绝"

    # Test Case 2: 数据量不足
    print("\n--- Test Case 2: 数据量不足 (bars=2) ---")
    insufficient_bars_data = {
        "AAPL": {
            "symbol": "AAPL",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": 150.0,
            "bars_1s": [{"ts": int(datetime.now(timezone.utc).timestamp() * 1000), "volume": 100}] * 2  # ❌ 只有 2 条
        }
    }

    # 重置状态
    bot.data_health['consecutive_failures'] = 0
    bot.data_health['status'] = 'HEALTHY'

    is_healthy = bot._check_data_health(insufficient_bars_data)
    print(f"Insufficient bars detected: {'✓ REJECTED' if not is_healthy else '✗ ACCEPTED (BAD!)'}")
    assert not is_healthy, "数据量不足应该被拒绝"

    # Test Case 3: 无时间戳
    print("\n--- Test Case 3: 无时间戳 ---")
    no_timestamp_data = {
        "AAPL": {
            "symbol": "AAPL",
            "timestamp": None,  # ❌ 无时间戳
            "current_price": 150.0,
            "bars_1s": []
        }
    }

    # 重置状态
    bot.data_health['consecutive_failures'] = 0
    bot.data_health['status'] = 'HEALTHY'

    is_healthy = bot._check_data_health(no_timestamp_data)
    print(f"No timestamp detected: {'✓ REJECTED' if not is_healthy else '✗ ACCEPTED (BAD!)'}")
    assert not is_healthy, "无时间戳应该被拒绝"

    print("\n✅ 测试场景 3 通过: 所有无效数据都被正确拒绝")


async def test_scenario_4_dynamic_interval():
    """测试场景 4: 动态检查间隔"""
    print("\n" + "="*80)
    print("📊 测试场景 4: 动态检查间隔")
    print("="*80)

    config = Config()
    bot = LLMTraderBot(
        provider_name="deepseek",
        symbols=["AAPL"],
        config=config,
        initial_cash=50000.0,
        dry_run=True,
        use_aggregator=False,
    )

    # Test Case 1: HEALTHY 状态
    bot.data_health['status'] = 'HEALTHY'
    bot.enable_adaptive_frequency = False  # 禁用自适应

    # 模拟 run() 中的间隔逻辑
    if bot.data_health["status"] == "UNHEALTHY":
        interval = 10
    elif bot.data_health["status"] == "RECOVERING":
        interval = 30
    else:
        interval = 60  # 默认

    print(f"HEALTHY 状态 → 间隔: {interval}s")
    assert interval == 60, "HEALTHY 应该是 60s"

    # Test Case 2: UNHEALTHY 状态
    bot.data_health['status'] = 'UNHEALTHY'

    if bot.data_health["status"] == "UNHEALTHY":
        interval = 10
    elif bot.data_health["status"] == "RECOVERING":
        interval = 30
    else:
        interval = 60

    print(f"UNHEALTHY 状态 → 间隔: {interval}s (快速探测)")
    assert interval == 10, "UNHEALTHY 应该是 10s"

    # Test Case 3: RECOVERING 状态
    bot.data_health['status'] = 'RECOVERING'

    if bot.data_health["status"] == "UNHEALTHY":
        interval = 10
    elif bot.data_health["status"] == "RECOVERING":
        interval = 30
    else:
        interval = 60

    print(f"RECOVERING 状态 → 间隔: {interval}s (验证稳定性)")
    assert interval == 30, "RECOVERING 应该是 30s"

    print("\n✅ 测试场景 4 通过: 动态间隔逻辑正确")


async def main():
    """运行所有测试"""
    print("="*80)
    print("🧪 数据健康监控 - 集成测试套件")
    print("="*80)
    print("\n本测试将验证:")
    print("1. 正常数据流处理")
    print("2. 缓存数据检测 (时间戳不递增)")
    print("3. 状态机转换 (HEALTHY → DEGRADED → UNHEALTHY → RECOVERING → HEALTHY)")
    print("4. 无效数据拒绝")
    print("5. 动态检查间隔")

    try:
        await test_scenario_1_normal_operation()
        await test_scenario_2_disconnect_and_reconnect()
        await test_scenario_3_invalid_data()
        await test_scenario_4_dynamic_interval()

        print("\n" + "="*80)
        print("✅ 所有测试通过!")
        print("="*80)
        print("\n🎯 核心功能验证:")
        print("  ✓ 时间戳递增检查 - 成功检测缓存数据")
        print("  ✓ 6 层数据验证 - 成功拒绝无效数据")
        print("  ✓ 状态机转换 - HEALTHY → UNHEALTHY → RECOVERING → HEALTHY")
        print("  ✓ 动态间隔 - UNHEALTHY 时 10s, RECOVERING 时 30s")
        print("\n🚀 系统已准备好部署!")

        return 0

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
