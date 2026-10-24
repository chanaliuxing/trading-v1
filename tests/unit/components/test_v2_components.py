import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from libs.strategy.components.market_fabric import MarketFabric, OhlcvBar
from libs.strategy.components.indicator_lib import IndicatorLib
from libs.strategy.components.execution_guard import ExecutionGuard

@pytest.mark.asyncio
async def test_market_fabric_tick_consumption():
    """验证 Tick 重放逻辑，确保在高频积压时不丢采样点"""
    lock = asyncio.Lock()
    fabric = MarketFabric(MagicMock(), {}, lock, ["QQQ"])
    
    # 模拟 3 个 Tick 积压
    ts = int(datetime.now().timestamp() * 1000)
    fabric.ticks_1s["QQQ"].append((ts - 2000, 400.0))
    fabric.ticks_1s["QQQ"].append((ts - 1000, 400.1))
    fabric.ticks_1s["QQQ"].append((ts, 400.2))
    
    # 第一次消费
    ticks = list(fabric.consume_new_ticks("QQQ"))
    assert len(ticks) == 3
    assert ticks[0][1] == 400.0
    
    # 第二次消费（无新数据）
    assert len(list(fabric.consume_new_ticks("QQQ"))) == 0
    
    # 新增一个点
    fabric.ticks_1s["QQQ"].append((ts + 1000, 400.3))
    ticks = list(fabric.consume_new_ticks("QQQ"))
    assert len(ticks) == 1
    assert ticks[0][1] == 400.3

@pytest.mark.asyncio
async def test_indicator_lib_optional_semantics():
    """验证 IndicatorLib 在数据不足时严格返回 None 而非 0.0"""
    # 只有 2 根 Bar，计算 14 周期 ATR
    bars = [MagicMock(close=100), MagicMock(close=101)]
    assert IndicatorLib.atr(bars, 14) is None
    
    # 零成交量的 VWAP
    assert IndicatorLib.vwap([]) is None

@pytest.mark.asyncio
async def test_execution_guard_deduplication():
    """验证订单去重逻辑，防止部分成交导致计数翻倍"""
    lock = asyncio.Lock()
    guard = ExecutionGuard(MagicMock(), {}, lock, max_trades=1)
    
    # 模拟同一笔订单的两次成交回报 (Partial Fills)
    update1 = MagicMock(state="FILLED", broker_order_id="ORDER_101", symbol="TQQQ")
    update2 = MagicMock(state="FILLED", broker_order_id="ORDER_101", symbol="TQQQ")
    
    await guard.on_order_update(update1)
    assert guard.state["success_count"] == 1
    
    await guard.on_order_update(update2)
    # 计数器不应增加
    assert guard.state["success_count"] == 1
