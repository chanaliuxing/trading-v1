import os
import sys
import asyncio
from datetime import datetime, time
from unittest.mock import MagicMock, AsyncMock, patch
from zoneinfo import ZoneInfo
from pathlib import Path

# CRITICAL: Fix sys.path BEFORE any local imports
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from libs.strategy import StrategyParams
# Pytest rootdir is `tests/`, which contains namespace packages named `strategies/`.
# Ensure we import the real strategy package from repo root.
if "strategies" in sys.modules:
    del sys.modules["strategies"]
from strategies.qqq_v2.strategy import QQQIntradayStrategyV2

ET = ZoneInfo("America/New_York")

@pytest.fixture
def mock_full_ctx():
    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = MagicMock()
    ctx.strategy_id = "qqq_v2_integration"
    ctx.engine = MagicMock()
    ctx.position = AsyncMock(return_value=None)
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    return ctx

@pytest.mark.asyncio
async def test_qqq_v2_full_day_cycle_simulation(mock_full_ctx):
    """模拟 QQQ V2 策略的全天生命周期：入场 -> 持仓管理 -> 强制平仓"""
    params = StrategyParams()
    params.extras = {
        "notional_usd": 10000.0,
        "loop_interval_sec": 0.01,
        "diag_log_path": "/tmp/qqq_v2_diag.jsonl"
    }
    
    strategy = QQQIntradayStrategyV2(mock_full_ctx, params)
    
    # 1. 初始化
    with patch("strategies.qqq_v2.strategy.StateMemex") as mock_memex_cls, \
         patch("strategies.qqq_v2.strategy.MarketFabric") as mock_fabric_cls, \
         patch("strategies.qqq_v2.strategy.asyncio.create_task") as mock_create_task, \
         patch("strategies.qqq_v2.strategy.IndicatorLib") as mock_ind_cls, \
         patch("strategies.qqq_v2.strategy.StrategicAnalyzer") as mock_analyzer_cls, \
         patch("strategies.qqq_v2.strategy.Observability") as mock_obs_cls, \
         patch("strategies.qqq_v2.strategy.ExecutionGuard") as mock_guard_cls, \
         patch("strategies.qqq_v2.strategy.ProtectiveShield") as mock_shield_cls, \
         patch("strategies.qqq_v2.strategy.RealityAuditor") as mock_auditor_cls, \
         patch("strategies.qqq_v2.strategy.HistoryProvider"), \
         patch("strategies.qqq_v2.strategy.QQQDataFabric") as mock_qqq_fabric_cls, \
         patch("strategies.qqq_v2.strategy.EntryGatekeeper") as mock_gatekeeper_cls, \
         patch("strategies.qqq_v2.strategy.PositionSupervisor") as mock_supervisor_cls:

        mock_memex = mock_memex_cls.return_value
        mock_memex.initialize = AsyncMock()
        mock_memex.save = AsyncMock()

        def _fake_create_task(coro, *args, **kwargs):
            try:
                coro.close()
            except Exception:
                pass
            return MagicMock()

        mock_create_task.side_effect = _fake_create_task

        mock_fabric = mock_fabric_cls.return_value
        mock_fabric.initialize = AsyncMock()
        mock_fabric.ingest = AsyncMock()
        mock_fabric.stop = AsyncMock()
        mock_fabric.get_last_price.return_value = 50.0
        mock_fabric.get_anchored_vwap.return_value = 50.0
        mock_fabric.get_v3m.return_value = 1.0
        mock_fabric.get_freshness_age.return_value = 0.5
        mock_fabric.consume_new_ticks.return_value = []
        mock_fabric.bars_5m = {"TQQQ": [], "SQQQ": []}

        mock_ind = mock_ind_cls.return_value
        mock_ind.atr.return_value = 0.3

        mock_analyzer = mock_analyzer_cls.return_value
        mock_analyzer.reset_daily = MagicMock()
        mock_analyzer.update_toxic_count = MagicMock()
        mock_analyzer.collect_seed_sample = MagicMock()
        mock_analyzer.lock_at_0945 = MagicMock()

        mock_obs = mock_obs_cls.return_value
        mock_obs.write_snapshot = MagicMock()

        mock_guard = mock_guard_cls.return_value
        mock_guard.sync_with_broker = AsyncMock()
        mock_guard.on_order_update = AsyncMock()
        mock_guard.can_enter = MagicMock(return_value=True)

        mock_shield_cls.return_value.manage_stop = AsyncMock(return_value=(None, "noop"))

        mock_auditor = mock_auditor_cls.return_value
        mock_auditor.start = AsyncMock()
        mock_auditor.stop = AsyncMock()

        mock_qqq_fabric = mock_qqq_fabric_cls.return_value
        mock_qqq_fabric.initialize = AsyncMock()
        mock_qqq_fabric.persist_atr_seeds = MagicMock()

        mock_gatekeeper = mock_gatekeeper_cls.return_value
        mock_gatekeeper.emit_diagnostic = AsyncMock()
        mock_gatekeeper.can_enter = AsyncMock(return_value=(False, "no_signal", None, None))

        mock_supervisor = mock_supervisor_cls.return_value

        async def _enter(exec_symbol, side, qty, atr_pre, current_price):
            strategy.persistent_state["is_traded"] = True
            strategy.persistent_state["exec_symbol"] = exec_symbol
            strategy.persistent_state["side"] = side
            strategy.persistent_state["atr_pre"] = {"TQQQ": atr_pre, "SQQQ": atr_pre}
            await mock_full_ctx.submit_intent(MagicMock(target_qty=float(qty)))

        async def _manage(*args, **kwargs):
            await mock_full_ctx.submit_intent(MagicMock(target_qty=0.0))

        async def _flatten(reason):
            await mock_full_ctx.submit_intent(MagicMock(target_qty=0.0))

        mock_supervisor.enter = AsyncMock(side_effect=_enter)
        mock_supervisor.manage = AsyncMock(side_effect=_manage)
        mock_supervisor.flatten = AsyncMock(side_effect=_flatten)

        await strategy.initialize()
        strategy.persistent_state["atr_pre"] = {"TQQQ": 0.35, "SQQQ": 0.30}

        # 2. 模拟入场 (10:00 AM)
        test_now = datetime.now(ET).replace(hour=10, minute=0, second=0)
        strategy.gatekeeper.can_enter = AsyncMock(return_value=(True, "test_signal", "LONG", 50.0))
        strategy.guard.can_enter = MagicMock(return_value=True)

        await strategy._run_loop_step(test_now)
        mock_full_ctx.submit_intent.assert_called()

        # 3. 模拟持仓管理 (11:00 AM)
        mock_fabric.get_last_price.return_value = 52.0
        await strategy._run_loop_step(test_now.replace(hour=11))
        assert mock_full_ctx.submit_intent.call_count >= 2

        # 4. 模拟强制平仓 (15:59 PM)
        test_eod = test_now.replace(hour=15, minute=59, second=1)
        await strategy._run_loop_step(test_eod)

        last_call_args = mock_full_ctx.submit_intent.call_args[0][0]
        assert last_call_args.target_qty == 0.0
