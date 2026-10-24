import pytest
import asyncio
from unittest.mock import MagicMock
from libs.strategy.components.protective_shield import ProtectiveShield

def test_shield_hard_cap_and_breakeven():
    state = {}
    mock_ctx = MagicMock()
    mock_ctx.log = MagicMock()
    mock_ctx.strategy_id = "test_strategy"
    lock = asyncio.Lock()
    
    shield = ProtectiveShield(mock_ctx, state, lock, stop_atr_mult=2.5, widening_cap_mult=1.5)
    
    # 1. 初始止损计算
    # Entry 100, ATR 0.4 -> Dist = 1.0 (2.5*0.4)
    # Max Dist = 1.5
    stop_px = shield.compute_initial_stop(100.0, 0.4)
    assert stop_px == 99.0
    assert state["stop_dist_max"] == 1.5
    
    # 2. 模拟 ATR 扩大到 1.0 (2.5 * 1.0 = 2.5)
    # 应该被硬上限 1.5 挡住
    loop = asyncio.get_event_loop()
    new_stop, reason = loop.run_until_complete(shield.manage_stop(99.5, atr_live=1.0))
    assert state["stop_dist_cur"] == 1.5
    assert new_stop == 98.5 # 100 - 1.5
    assert reason == "atr_widening"

    # 3. 模拟盈利触发挪盾 (Breakeven)
    # Price hits 101.5 (Profit 1.5 > 1.0)
    # Stop should move to Entry + 0.2 = 100.2
    new_stop, reason = loop.run_until_complete(shield.manage_stop(101.5, atr_live=0.4))
    assert new_stop == 100.2
    assert reason == "breakeven_shield"
    assert state["breakeven_done"] is True
