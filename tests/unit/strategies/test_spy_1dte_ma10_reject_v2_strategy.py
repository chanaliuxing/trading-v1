from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from libs.strategy import StrategyParams
from strategies.spy_1dte_ma10_reject_v2_strategy import SPY1DTEMA10RejectV2Strategy


ET = ZoneInfo("America/New_York")


class _DummyLogger:
    def __init__(self):
        self.warning = MagicMock()
        self.info = MagicMock()
        self.error = MagicMock()
        self.debug = MagicMock()


def _make_strategy(extras=None):
    ctx = MagicMock()
    ctx.log = _DummyLogger()
    ctx.engine = MagicMock()
    ctx.engine.audit_writer = MagicMock()
    ctx.subscribe_features = AsyncMock()
    ctx.enable_realtime_feature_engine = AsyncMock()
    ctx.disable_realtime_feature_engine = AsyncMock()
    ctx.submit_intent = AsyncMock()
    ctx.portfolio = AsyncMock(return_value={"positions": []})
    ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.strategy_id = "spy_1dte_ma10_reject_0324_001"
    params = StrategyParams()
    params.extras = extras or {}
    return SPY1DTEMA10RejectV2Strategy(ctx, params), ctx


@pytest.mark.asyncio
async def test_initialize_defaults_to_spy():
    strat, _ctx = _make_strategy({"kernel_mode": "delegate"})
    await strat.initialize()
    delegate = strat.kernel.delegate
    assert delegate is not None
    assert delegate.underlying == "SPY"
    assert str(delegate.risk_symbol) == "OPT:SPY:1DTE"


@pytest.mark.asyncio
async def test_initialize_uses_pure_plugin_when_configured():
    strat, _ctx = _make_strategy({"kernel_mode": "pure_plugin"})
    await strat.initialize()
    assert strat.kernel.delegate is None


@pytest.mark.asyncio
async def test_initialize_emits_system_startup_audit_events():
    strat, ctx = _make_strategy({"kernel_mode": "pure_plugin"})
    await strat.initialize()

    emits = ctx.engine.audit_writer.emit.call_args_list
    assert len(emits) == 2

    started = emits[0].kwargs
    ready = emits[1].kwargs

    assert started["domain"] == "system"
    assert started["stage"] == "strategy_started"
    assert started["strategy_id"] == "spy_1dte_ma10_reject_0324_001"
    assert started["status"] == "started"
    assert started["reason"] == "initialize_begin"

    assert ready["domain"] == "system"
    assert ready["stage"] == "strategy_ready"
    assert ready["strategy_id"] == "spy_1dte_ma10_reject_0324_001"
    assert ready["status"] == "ready"
    assert ready["reason"] == "initialize_complete"


@pytest.mark.asyncio
async def test_sync_position_does_not_pause_on_foreign_underlying_open_option_order():
    strat, ctx = _make_strategy({"preload_history_bars": 0, "kernel_mode": "delegate"})
    await strat.initialize()
    delegate = strat.kernel.delegate
    assert delegate is not None
    ctx.open_orders = AsyncMock(return_value=[MagicMock(symbol="OPT:QQQ:20260326:530:C")])

    now = datetime(2026, 3, 25, 10, 0, 0, tzinfo=ET)
    bar = {"close": 600.0, "vwap": 600.0}
    paused = await delegate._sync_and_manage_position(now, bar)

    assert paused is False


@pytest.mark.asyncio
async def test_sync_position_keeps_pending_entry_only_for_strategy_owned_order():
    strat, ctx = _make_strategy({"preload_history_bars": 0, "kernel_mode": "delegate"})
    await strat.initialize()
    delegate = strat.kernel.delegate
    assert delegate is not None
    delegate._pos = MagicMock(opt_symbol="OPT:SPY:20260326:590:C", filled=False, last_exit_reason=None)
    ctx.position = AsyncMock(return_value=SimpleNamespace(pos_qty=0.0, avg_px=0.0))
    ctx.open_orders = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="OPT:SPY:20260326:590:C",
                strategy_id="spy_other_strategy_001",
                order_ref="entry:spy_other_strategy_001:trace-1",
                status="Submitted",
            )
        ]
    )

    paused = await delegate._sync_and_manage_position(datetime(2026, 3, 25, 10, 1, tzinfo=ET), {"close": 590.0, "vwap": 589.9})

    assert paused is False
    assert delegate._pos is None
