import asyncio
from types import SimpleNamespace
from datetime import datetime, time
from unittest.mock import MagicMock, AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import StrategyParams, OrderUpdate
from strategies.qqq_v2.strategy import QQQIntradayStrategyV2

ET = ZoneInfo("America/New_York")

@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.strategy_id = "qqq_v2_test"
    ctx.engine = MagicMock()
    ctx.position = AsyncMock(return_value=None)
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    return ctx

@pytest.mark.asyncio
async def test_v2_full_reset_and_sampling_alignment(mock_ctx):
    """
    集成验证：
    1. 验证 Daily Reset 后的组件重置。
    2. 验证 Tick 采样后的价格原子透传。
    """
    params = StrategyParams()
    params.extras = {"loop_interval_sec": 0.01}
    strategy = QQQIntradayStrategyV2(mock_ctx, params)
    
    with patch("strategies.qqq_v2.strategy.StateMemex") as mock_memex_cls, \
         patch("strategies.qqq_v2.strategy.MarketFabric") as mock_fabric_cls, \
         patch("strategies.qqq_v2.strategy.HistoryProvider"), \
         patch("strategies.qqq_v2.strategy.QQQDataFabric") as mock_qqq_fabric_cls:
        
        # 准备 Mock 环境
        mock_memex = mock_memex_cls.return_value
        mock_memex.initialize = AsyncMock()
        mock_memex.save = AsyncMock()
        
        mock_fabric = mock_fabric_cls.return_value
        mock_fabric.initialize = AsyncMock()
        mock_fabric.ingest = AsyncMock()
        mock_fabric.consume_new_ticks.return_value = [(1000, 400.0)] # 1 个 Tick
        mock_fabric.get_anchored_vwap.return_value = 399.5
        mock_fabric.get_last_price.return_value = 400.0
        mock_fabric.get_freshness_age.return_value = 0.5 # New: Return float for comparison
        mock_fabric.get_v3m.return_value = 0.5
        mock_fabric.bars_1m = {"QQQ": [], "QQEW": []}
        mock_fabric.bars_5m = {"TQQQ": [], "SQQQ": []}
        
        mock_qqq_fabric = mock_qqq_fabric_cls.return_value
        mock_qqq_fabric.initialize = AsyncMock()
        
        await strategy.initialize()
        
        # 手动初始化必要的状态键
        strategy.persistent_state["success_count"] = 0
        strategy.persistent_state["is_traded"] = False
        strategy.persistent_state["pending_decision_id"] = None
        strategy.persistent_state["atr_pre"] = {"TQQQ": 0.40, "SQQQ": 0.35}
        
        # 1. 验证日期重置
        test_now = datetime.now(ET)
        strategy.persistent_state["_day"] = "1970-01-01" # 强制过期
        strategy.persistent_state["is_traded"] = True
        
        await strategy._run_loop_step(test_now)
        
        # 应该触发了 Reset
        assert strategy.persistent_state["_day"] == test_now.strftime("%Y-%m-%d")
        assert strategy.persistent_state["is_traded"] is False # 计数器被清空
        
        # 2. 验证原子透传 (Causality Alignment)
        # 模拟 Gatekeeper 发现了一个 400.5 的入场信号
        strategy.gatekeeper.can_enter = AsyncMock(return_value=(True, "ok", "LONG", 400.5))
        strategy.guard.can_enter = MagicMock(return_value=True)
        strategy.supervisor.enter = AsyncMock()
        
        await strategy._run_loop_step(test_now)
        
        # 核心断言：Supervisor 接收到的价格必须是 400.5
        strategy.supervisor.enter.assert_called_once()
        args = strategy.supervisor.enter.call_args[0]
        assert args[4] == 400.5 # signal_px
        
        await strategy.auditor.stop()
        if hasattr(strategy, "_main_task"): strategy._main_task.cancel()


def _build_initialized_v2_strategy(mock_ctx) -> QQQIntradayStrategyV2:
    params = StrategyParams()
    params.extras = {"loop_interval_sec": 0.01}
    strategy = QQQIntradayStrategyV2(mock_ctx, params)
    strategy.logger = mock_ctx.log
    strategy.state_lock = asyncio.Lock()
    strategy.persistent_state = {
        "_day": "2026-05-01",
        "_diag_path": "runtime/state/qqq_v2_diag.jsonl",
        "_init_at": "2026-05-01T09:30:00-04:00",
        "_version": "test",
        "atr_pre": {"TQQQ": 0.40, "SQQQ": 0.35},
        "data_integrity_warning": True,
        "exec_symbol": "TQQQ",
        "is_traded": True,
        "side": "LONG",
    }
    strategy.qqq_fabric = SimpleNamespace(
        initialize=AsyncMock(),
        persist_atr_seeds=MagicMock(),
    )
    strategy.fabric = SimpleNamespace(
        reset=MagicMock(),
        ingest=AsyncMock(),
        consume_new_ticks=MagicMock(return_value=[]),
        get_anchored_vwap=MagicMock(return_value=99.5),
        get_freshness_age=MagicMock(return_value=0.1),
        get_last_price=MagicMock(return_value=100.0),
        get_v3m=MagicMock(return_value=0.2),
        bars_5m={"TQQQ": [1.0, 2.0, 3.0], "SQQQ": [1.0, 2.0, 3.0]},
    )
    strategy.gatekeeper = SimpleNamespace(
        emit_diagnostic=AsyncMock(),
        can_enter=AsyncMock(return_value=(False, "integrity_failure", None, 0.0)),
    )
    strategy.analyzer = SimpleNamespace(
        reset_daily=MagicMock(),
        update_toxic_count=MagicMock(),
        collect_seed_sample=MagicMock(),
        lock_at_0945=MagicMock(),
    )
    strategy.guard = SimpleNamespace(can_enter=MagicMock(return_value=True))
    strategy.shield = SimpleNamespace()
    strategy.auditor = SimpleNamespace()
    strategy.supervisor = SimpleNamespace(
        enter=AsyncMock(),
        flatten=AsyncMock(),
        manage=AsyncMock(),
    )
    strategy.indicator_lib = SimpleNamespace(atr=MagicMock(return_value=0.25))
    strategy.obs = SimpleNamespace(write_snapshot=MagicMock())
    strategy.memex = SimpleNamespace(save=AsyncMock())
    return strategy


@pytest.mark.asyncio
async def test_v2_data_integrity_warning_blocks_entry_but_keeps_management(mock_ctx):
    strategy = _build_initialized_v2_strategy(mock_ctx)

    await strategy._run_loop_step(datetime(2026, 5, 1, 10, 0, tzinfo=ET))

    strategy.fabric.ingest.assert_awaited_once()
    strategy.gatekeeper.can_enter.assert_awaited_once()
    strategy.supervisor.enter.assert_not_awaited()
    strategy.supervisor.manage.assert_awaited_once_with(100.0, 99.5, 0.25)
    strategy.supervisor.flatten.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_data_integrity_warning_keeps_eod_flatten(mock_ctx):
    strategy = _build_initialized_v2_strategy(mock_ctx)

    await strategy._run_loop_step(datetime(2026, 5, 1, 15, 59, tzinfo=ET))

    strategy.fabric.ingest.assert_awaited_once()
    strategy.supervisor.manage.assert_awaited_once_with(100.0, 99.5, 0.25)
    strategy.supervisor.flatten.assert_awaited_once_with("EOD_FORCE_FLATTEN")
    strategy.supervisor.enter.assert_not_awaited()
