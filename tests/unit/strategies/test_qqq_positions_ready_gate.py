import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import StrategyContext, StrategyParams
from strategies.qqq_intraday_strategy import QQQIntradayStrategy


TEST_TZ = ZoneInfo("America/New_York")


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


def _make_ib(*, ok: bool = True):
    ib = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    if ok:
        ib.reqPositionsAsync = AsyncMock(return_value=[])
    else:
        ib.reqPositionsAsync = AsyncMock(side_effect=RuntimeError("IB not ready"))
    return ib


def _make_strategy(*, ib_ok: bool = True) -> QQQIntradayStrategy:
    ctx = MagicMock(spec=StrategyContext)
    ctx.log = _DummyLogger()
    ctx.metrics = lambda *args, **kwargs: None
    ctx.strategy_id = "qqq_intraday_001"
    ctx.position = AsyncMock(return_value=None)
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()

    engine = MagicMock()
    mds = MagicMock()
    ib_adapter = MagicMock()
    ib_adapter.ib = _make_ib(ok=ib_ok)
    mds.ib_adapter = ib_adapter
    engine.market_data_service = mds
    ctx.engine = engine

    params = StrategyParams()
    params.extras = {
        "enable_observability_file": False,
        "fail_fast_enabled": False,
        # Keep tests snappy/deterministic.
        "broker_positions_refresh_interval_sec": 10.0,
        "broker_positions_refresh_timeout_sec": 0.5,
        "startup_gate_warn_interval_sec": 0.5,
        "startup_force_sync_interval_sec": 0.5,
    }
    strat = QQQIntradayStrategy(ctx, params)
    strat._day = datetime.now(TEST_TZ).strftime("%Y-%m-%d")
    strat._running = True
    strat._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    return strat


@pytest.mark.asyncio
async def test_refresh_ib_positions_marks_ready_and_respects_interval():
    strat = _make_strategy(ib_ok=True)

    ok1 = await strat._refresh_ib_positions_best_effort(force=True)
    ok2 = await strat._refresh_ib_positions_best_effort(force=False)

    assert ok1 is True
    assert ok2 is True
    assert strat._broker_positions_ready is True
    # Second call should be skipped by interval gating.
    assert strat.ctx.engine.market_data_service.ib_adapter.ib.reqPositionsAsync.await_count == 1


@pytest.mark.asyncio
async def test_startup_gate_blocks_until_positions_ready():
    strat = _make_strategy(ib_ok=False)
    strat._sync_position_state = AsyncMock()
    now = datetime.now(TEST_TZ)
    strat._startup_positions_ready = False
    strat._startup_positions_deadline_mono = asyncio.get_running_loop().time() + 10.0

    ready1 = await strat._startup_positions_gate(now)
    assert ready1 is False
    assert strat._startup_positions_ready is False

    # Flip IB to ready and ensure gate opens.
    strat.ctx.engine.market_data_service.ib_adapter.ib.reqPositionsAsync = AsyncMock(return_value=[])
    ready2 = await strat._startup_positions_gate(now)
    assert ready2 is True
    assert strat._startup_positions_ready is True


@pytest.mark.asyncio
async def test_run_loop_skips_entry_and_manage_until_gate_ready():
    strat = _make_strategy(ib_ok=True)

    # Patch out everything except the gate decision.
    strat._daily_reset_if_needed = MagicMock()
    strat._ingest_required = AsyncMock()
    strat._maybe_retry_seed_warmup = AsyncMock()
    strat._maybe_lock_seed = MagicMock()
    strat._refresh_atr_pre_by_schedule = MagicMock()
    strat._maybe_persist_daily_atr_seed = MagicMock()
    strat._maybe_write_observability_file = MagicMock()
    strat._maybe_emit_heartbeat = MagicMock()
    strat._fail_fast_if_missing = MagicMock()
    strat._update_crosscount = MagicMock()
    strat._lock_amp_params = AsyncMock()
    strat._force_flatten_1559 = AsyncMock()
    strat._sync_position_state = AsyncMock()

    strat._maybe_enter = AsyncMock()
    strat._manage_position = AsyncMock()

    # First: gate blocks.
    strat._startup_positions_gate = AsyncMock(return_value=False)
    task = asyncio.create_task(strat._run_loop(0.1))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    strat._maybe_enter.assert_not_called()
    strat._manage_position.assert_not_called()

    # Second: gate allows.
    strat._maybe_enter.reset_mock()
    strat._manage_position.reset_mock()
    strat._startup_positions_gate = AsyncMock(return_value=True)
    task2 = asyncio.create_task(strat._run_loop(0.1))
    await asyncio.sleep(0.15)
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2
    assert strat._maybe_enter.await_count >= 1
    assert strat._manage_position.await_count >= 1


@pytest.mark.asyncio
async def test_current_exec_qty_uses_broker_on_large_mismatch():
    strat = _make_strategy(ib_ok=True)

    strat.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=201.0))
    strat._broker_position_qty = AsyncMock(return_value=140.0)
    strat.params.extras["position_qty_mismatch_abs"] = 0.5

    qty = await strat._current_exec_qty_for_symbol("TQQQ")
    assert qty == 140.0


@pytest.mark.asyncio
async def test_flatten_forces_positions_refresh_first():
    strat = _make_strategy(ib_ok=True)
    strat._exec_symbol = "TQQQ"
    strat._is_traded = True
    strat._side = "LONG"

    strat._refresh_ib_positions_best_effort = AsyncMock(return_value=True)
    strat._current_exec_qty = AsyncMock(return_value=10.0)
    strat.ctx.open_orders = AsyncMock(return_value=[])
    strat.ctx.submit_intent = AsyncMock()

    await strat._flatten("stop_hit")

    strat._refresh_ib_positions_best_effort.assert_awaited()
    call = strat._refresh_ib_positions_best_effort.await_args_list[0]
    assert call.kwargs.get("force") is True
    strat.ctx.submit_intent.assert_awaited()

