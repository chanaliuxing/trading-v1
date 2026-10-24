import json
import asyncio
import time as time_mod
import pytest
from datetime import datetime, time, timedelta
from unittest.mock import MagicMock, AsyncMock
from zoneinfo import ZoneInfo

from libs.strategy import StrategyContext, StrategyParams, OrderUpdate
import strategies.qqq_intraday_strategy as qmod
from strategies.qqq_intraday_strategy import QQQIntradayStrategy

# 使用纽约时区进行测试
TEST_TZ = ZoneInfo("America/New_York")

class _DummyLogger:
    def info(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass
    def debug(self, *args, **kwargs): pass


class _AuditWriter:
    def __init__(self):
        self.records = []

    def emit(self, **kwargs):
        self.records.append(kwargs)


class _FakePg:
    def __init__(self, row=None):
        self.row = row
        self.fetchone_calls = []
        self.execute_calls = []

    async def fetchone(self, *args):
        self.fetchone_calls.append(args)
        return self.row

    async def execute(self, *args):
        self.execute_calls.append(args)
        return "UPDATE 1"

def _make_strategy():
    ctx = MagicMock(spec=StrategyContext)
    ctx.log = _DummyLogger()
    ctx.metrics = lambda *args, **kwargs: None
    ctx.strategy_id = "qqq_intraday_001"
    ctx.engine = MagicMock()
    ctx.position = AsyncMock(return_value=None)
    ctx.open_orders = AsyncMock(return_value=[])
    ctx.submit_intent = AsyncMock()
    ctx.engine.audit_writer = _AuditWriter()
    
    params = StrategyParams()
    params.extras = {
        "enable_observability_file": False,
        "fail_fast_enabled": False,
        "submit_exchange_stop_intent": True
    }
    strat = QQQIntradayStrategy(ctx, params)
    strat._day = datetime.now(TEST_TZ).strftime("%Y-%m-%d")
    strat._running = True
    return strat

@pytest.mark.asyncio
async def test_ny_eod_flatten():
    """验证纽约时间 15:59 触发强制平仓"""
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._flatten = AsyncMock()
    
    # 模拟纽约时间 15:59
    now = datetime(2026, 2, 26, 15, 59, 0, tzinfo=TEST_TZ)  # Thursday
    await strategy._force_flatten_1559(now)
    
    strategy._flatten.assert_called_with("force_flatten_1559_EOD")

@pytest.mark.asyncio
async def test_stop_widening_hard_cap():
    """验证止损放宽严格限制在 1.5 倍初始距离"""
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._stop_dist_pre = 2.0  # 初始距离 2.0
    
    # 模拟一个巨大的 ATR，导致计算出的距离为 10.0
    strategy._atr_5m = MagicMock(return_value=4.0) # 2.5 * 4 = 10.0
    
    now = datetime.now(TEST_TZ).replace(hour=10, minute=5)
    await strategy._maybe_update_stop_by_atr(now)
    
    assert strategy._stop_dist_cur == 3.0
    assert strategy._stop_px == 97.0 # 100 - 3.0

@pytest.mark.asyncio
async def test_move_shield_anti_suicide():
    """验证挪盾逻辑 (利润 >= 1.0)"""
    strategy = _make_strategy()
    strategy.params.extras["enable_breakeven_shield"] = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._expected_entry_qty = 100
    strategy._stop_px = 98.0
    strategy._breakeven_done = False
    strategy._broker_position_qty = AsyncMock(return_value=100.0)
    # Ensure a live position exists so stop intent is actually emitted.
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=100.0))
    
    now = datetime(2026, 3, 2, 10, 10, tzinfo=TEST_TZ)

    # 场景 1：利润不足 (0.5 < 1.0)
    strategy._last_price = MagicMock(return_value=100.5)
    strategy.ctx.submit_intent.reset_mock()
    await strategy._manage_position(now)
    assert strategy._breakeven_done is False
    strategy.ctx.submit_intent.assert_not_called()

    # 场景 2：利润满足 (1.5 > 1.0)
    strategy._last_price = MagicMock(return_value=101.5)
    strategy.ctx.submit_intent.reset_mock()
    await strategy._manage_position(now)
    assert strategy._breakeven_done is True
    assert strategy._stop_px == 100.20
    strategy.ctx.submit_intent.assert_called()

@pytest.mark.asyncio
async def test_sqqq_stop_direction():
    """验证 SQQQ 止损计算依然使用减法 (Entry - Dist)"""
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._entry_px = 70.0
    strategy._atr_pre["SQQQ"] = 0.30
    
    # SQQQ 作为多头标的购买，止损在价格下方
    dist = 2.5 * 0.30 # 0.75
    strategy._stop_px = round(70.0 - 0.75, 2)
    
    assert strategy._stop_px == 69.25
    assert strategy._stop_px < strategy._entry_px

@pytest.mark.asyncio
async def test_audit_loop_kill_switch_rhythm():
    """验证 1.2s 频率审计及 8 次失败触发 Kill Switch (约 10 秒)"""
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._running = True
    strategy._flatten = AsyncMock()
    strategy._broker_position_qty = AsyncMock(return_value=1.0)
    # Ensure a live position exists so audit loop counts misses.
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=1.0))
    
    # 模拟 IBKR 没有单子
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    
    # 启动审计任务
    audit_task = asyncio.create_task(strategy._audit_stop_order_loop())
    
    # 等待约 10 秒 (1.2s * 8 = 9.6s)
    await asyncio.sleep(11.0)
    
    # 验证触发了 Kill Switch
    strategy._flatten.assert_called_with("STOP_LOSS_MISSING_KILL_SWITCH")
    audit_task.cancel()


@pytest.mark.asyncio
async def test_sync_position_grace_prevents_false_flat_after_entry():
    """验证入场后短暂宽限期可避免成交回报前被误判为 flat。"""
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._entry_px = 75.0
    strategy._pending_decision_id = "d_pending"
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy._entry_sync_grace_until_mono = asyncio.get_running_loop().time() + 60.0
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.open_orders = AsyncMock(return_value=[])

    await strategy._sync_position_state(force=True)

    # Should not clear active execution state during grace window.
    assert strategy._exec_symbol == "SQQQ"
    assert strategy._side == "SHORT"


@pytest.mark.asyncio
async def test_reconcile_live_position_rebuilds_stop_and_submits_protection():
    """重启后发现 live 仓位且无 stop 时，自动重建 stop 并补发保护单。"""
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = False
    strategy._exec_symbol = None
    strategy._side = None
    strategy._entry_px = None
    strategy._stop_px = None
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy._atr_pre["SQQQ"] = 0.30
    strategy._last_protection_ensure_mono = 0.0
    strategy._broker_position_qty = AsyncMock(return_value=50.0)

    async def _pos(symbol):
        if symbol == "SQQQ":
            return MagicMock(pos_qty=50.0, avg_px=75.2)
        return None

    strategy.ctx.position = AsyncMock(side_effect=_pos)
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._submit_protective_stop_intent = AsyncMock()
    strategy._audit_stop_order_loop = AsyncMock(return_value=None)

    await strategy._sync_position_state(force=True)

    assert strategy._is_traded is True
    assert strategy._exec_symbol == "SQQQ"
    assert strategy._side == "SHORT"
    assert strategy._stop_px is not None
    strategy._submit_protective_stop_intent.assert_awaited()


@pytest.mark.asyncio
async def test_sync_position_live_does_not_increment_success_count():
    """live 持仓同步不应直接增加 success_count，只标记回合已真实持仓。"""
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._success_count = 0
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._entry_px = 75.2
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(side_effect=lambda s: MagicMock(pos_qty=50.0, avg_px=75.2) if s == "SQQQ" else None)
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._submit_protective_stop_intent = AsyncMock()
    strategy._audit_stop_order_loop = AsyncMock(return_value=None)

    await strategy._sync_position_state(force=True)

    assert strategy._success_count == 0
    assert strategy._round_live_confirmed is True


@pytest.mark.asyncio
async def test_run_loop_emits_observability_while_system_paused():
    strategy = _make_strategy()
    strategy._system_paused = True
    strategy._ingest_required = AsyncMock(return_value=None)
    strategy._maybe_retry_seed_warmup = AsyncMock(return_value=None)
    strategy._maybe_lock_seed = MagicMock()
    strategy._refresh_atr_pre_by_schedule = MagicMock()
    strategy._maybe_persist_daily_atr_seed = MagicMock()
    strategy._maybe_write_observability_file = MagicMock()
    strategy._maybe_emit_heartbeat = MagicMock()

    task = asyncio.create_task(strategy._run_loop(0.01))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert strategy._loop_last_stage == "system_paused"
    assert strategy._ingest_required.await_count >= 1
    assert strategy._maybe_retry_seed_warmup.await_count >= 1
    assert strategy._maybe_lock_seed.call_count >= 1
    assert strategy._refresh_atr_pre_by_schedule.call_count >= 1
    assert strategy._maybe_persist_daily_atr_seed.call_count >= 1
    assert strategy._maybe_write_observability_file.call_count >= 1
    assert strategy._maybe_emit_heartbeat.call_count >= 1


@pytest.mark.asyncio
async def test_audit_loop_non_stop_order_does_not_count_as_protected():
    """审计循环遇到非 STP 挂单时，不应判定为止损存在。"""
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._running = True
    strategy._audit_miss_count = 0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=1.0))

    async def _open_orders_once():
        strategy._running = False
        return [MagicMock(strategy_id=strategy.strategy_id, symbol="TQQQ", state="SUBMITTED", order_type="LMT", order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY", note="")]

    strategy.ctx.open_orders = AsyncMock(side_effect=_open_orders_once)
    strategy._flatten = AsyncMock()

    await strategy._audit_stop_order_loop()

    assert strategy._audit_miss_count >= 1
    strategy._flatten.assert_not_called()


@pytest.mark.asyncio
async def test_audit_loop_clears_stale_flattening_and_resumes_audit():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._running = True
    strategy.state["is_flattening"] = True
    strategy._flattening_started_mono = asyncio.get_running_loop().time() - 30.0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=1.0))

    async def _open_orders_once():
        strategy._running = False
        return []

    strategy.ctx.open_orders = AsyncMock(side_effect=_open_orders_once)
    strategy._flatten = AsyncMock()

    await strategy._audit_stop_order_loop()

    assert strategy.state.get("is_flattening") is False
    assert strategy._audit_miss_count >= 1
    strategy._flatten.assert_not_called()


@pytest.mark.asyncio
async def test_audit_loop_accepts_presubmitted_stop_state_case():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._running = True
    strategy._audit_miss_count = 0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=265.0))

    async def _open_orders_once():
        strategy._running = False
        return [
            MagicMock(
                strategy_id=strategy.strategy_id,
                symbol="SQQQ",
                state="PreSubmitted",
                order_type="STP",
                order_ref="qqqv1:qqq_intraday_001:SQQQ:STOP",
                note="protective_stop",
            )
        ]

    strategy.ctx.open_orders = AsyncMock(side_effect=_open_orders_once)
    strategy._flatten = AsyncMock()

    await strategy._audit_stop_order_loop()

    assert strategy._audit_miss_count == 0
    strategy._flatten.assert_not_called()


@pytest.mark.asyncio
async def test_audit_loop_defers_missing_count_during_recent_stop_submit_grace():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._running = True
    strategy._audit_miss_count = 2
    strategy.params.extras["stop_audit_recent_submit_grace_sec"] = 4.0
    strategy._last_stop_submit_mono = asyncio.get_running_loop().time()
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=81.0))

    async def _open_orders_once():
        strategy._running = False
        return []

    strategy._open_orders_for_protection_audit = AsyncMock(side_effect=_open_orders_once)
    strategy._flatten = AsyncMock()
    strategy._submit_protective_stop_intent = AsyncMock()

    await strategy._audit_stop_order_loop()

    assert strategy._audit_miss_count == 0
    strategy._submit_protective_stop_intent.assert_not_awaited()
    strategy._flatten.assert_not_called()

@pytest.mark.asyncio
async def test_audit_loop_recognizes_stop_from_order_update():
    """open_orders_view 返回 OrderUpdate 时，审计应能识别 STP 止损单。"""
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._running = True
    strategy._audit_miss_count = 0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=207.0))

    async def _open_orders_once():
        strategy._running = False
        return [
            OrderUpdate(
                order_id="oid-stop-1",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="SUBMITTED",
                order_type="STP",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:1",
                note="protective_stop",
                stop_px=47.28,
            )
        ]

    strategy.ctx.open_orders = AsyncMock(side_effect=_open_orders_once)
    strategy._flatten = AsyncMock()

    await strategy._audit_stop_order_loop()

    assert strategy._audit_miss_count == 0
    strategy._flatten.assert_not_called()


@pytest.mark.asyncio
async def test_audit_loop_startup_grace_handles_transient_not_traded_and_self_heals():
    """入场瞬间短暂 not_traded 不应让审计线程提前退出；应继续并触发补挂止损。"""
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = False
    strategy._exec_symbol = "TQQQ"
    strategy._audit_miss_count = 2
    strategy.params.extras["stop_audit_startup_grace_sec"] = 3.0
    strategy._flatten = AsyncMock()
    strategy._submit_protective_stop_intent = AsyncMock(side_effect=lambda *_: setattr(strategy, "_running", False))
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._current_exec_qty = AsyncMock(return_value=1.0)

    async def _flip_to_traded():
        await asyncio.sleep(0.05)
        strategy._is_traded = True

    asyncio.create_task(_flip_to_traded())
    await strategy._audit_stop_order_loop()

    strategy._submit_protective_stop_intent.assert_awaited_once_with("audit_self_heal")
    strategy._flatten.assert_not_called()


@pytest.mark.asyncio
async def test_position_qty_falls_back_to_broker_when_db_empty():
    strategy = _make_strategy()
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter.account = "DUO112752"
    strategy.ctx.engine.market_data_service.ib_adapter.ib = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter.ib.positions = MagicMock(
        return_value=[
            MagicMock(
                account="DUO112752",
                contract=MagicMock(symbol="SQQQ"),
                position=133.0,
            )
        ]
    )

    qty = await strategy._current_exec_qty_for_symbol("SQQQ")
    assert qty == 133.0


@pytest.mark.asyncio
async def test_position_qty_prefers_broker_when_ctx_stale_nonzero():
    strategy = _make_strategy()
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=138.0))
    strategy._broker_position_qty = AsyncMock(return_value=0.0)

    qty = await strategy._current_exec_qty_for_symbol("SQQQ")
    assert qty == 0.0


@pytest.mark.asyncio
async def test_position_qty_ctx_zero_uses_strategy_claim_not_full_broker_qty():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._planned_total_qty = 140
    strategy._expected_entry_qty = 140
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy._broker_position_qty = AsyncMock(return_value=280.0)

    qty = await strategy._current_exec_qty_for_symbol("TQQQ")
    assert qty == 140.0


@pytest.mark.asyncio
async def test_position_qty_ctx_zero_without_claim_does_not_hijack_broker_qty():
    strategy = _make_strategy()
    strategy._is_traded = False
    strategy._exec_symbol = None
    strategy._planned_total_qty = 0
    strategy._expected_entry_qty = 0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy._broker_position_qty = AsyncMock(return_value=280.0)

    qty = await strategy._current_exec_qty_for_symbol("TQQQ")
    assert qty == 0.0


@pytest.mark.asyncio
async def test_position_qty_ctx_zero_preserves_negative_broker_qty_for_safety():
    strategy = _make_strategy()
    strategy._is_traded = False
    strategy._exec_symbol = None
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=0.0))
    strategy._broker_position_qty = AsyncMock(return_value=-50.0)

    qty = await strategy._current_exec_qty_for_symbol("TQQQ")
    assert qty == -50.0


@pytest.mark.asyncio
async def test_ensure_live_protection_skip_when_stop_exists():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 48.18
    strategy._stop_px = 47.04
    strategy._last_protection_ensure_mono = 0.0

    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            MagicMock(
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="Submitted",
                order_type="STP",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP",
                note="protective_stop",
            )
        ]
    )
    strategy._submit_protective_stop_intent = AsyncMock()

    await strategy._ensure_live_protection(datetime.now(TEST_TZ))

    strategy._submit_protective_stop_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_protective_stop_dedupes_same_signature_within_window():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._stop_px = 47.04
    strategy.params.extras["stop_intent_dedupe_sec"] = 60.0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=207.0))
    strategy.ctx.submit_intent = AsyncMock()
    strategy._broker_position_qty = AsyncMock(return_value=207.0)

    await strategy._submit_protective_stop_intent("audit_self_heal")
    await strategy._submit_protective_stop_intent("position_reconcile_protection")

    assert strategy.ctx.submit_intent.await_count == 1


@pytest.mark.asyncio
async def test_protective_stop_submits_again_when_signature_changes():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._stop_px = 47.04
    strategy.params.extras["stop_intent_dedupe_sec"] = 60.0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=207.0))
    strategy.ctx.submit_intent = AsyncMock()
    strategy._broker_position_qty = AsyncMock(return_value=207.0)

    await strategy._submit_protective_stop_intent("audit_self_heal")
    strategy._stop_px = 47.20
    await strategy._submit_protective_stop_intent("stop_widened")

    assert strategy.ctx.submit_intent.await_count == 2
    env1 = strategy.ctx.submit_intent.await_args_list[0].args[0]
    env2 = strategy.ctx.submit_intent.await_args_list[1].args[0]
    assert env1.order_style.stop_px != env2.order_style.stop_px


@pytest.mark.asyncio
async def test_protective_stop_skips_when_inventory_is_short():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._stop_px = 71.65
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=-137.0))
    strategy.ctx.submit_intent = AsyncMock()

    await strategy._submit_protective_stop_intent("position_reconcile_protection")

    strategy.ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_protective_stop_submit_uncertain_arms_recovery_without_cleanup():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._stop_px = 47.04
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=50.0))
    strategy._broker_position_qty = AsyncMock(return_value=50.0)
    strategy._audit_stop_order_loop = AsyncMock(return_value=None)
    strategy._submit_protective_stop_without_cancelling_pipeline = AsyncMock(
        side_effect=RuntimeError("protective_stop_submit_uncertain:TQQQ:30.0")
    )
    strategy._mark_unsubmitted_timeout_order_terminal = AsyncMock(
        side_effect=AssertionError("protective stop uncertain state must not be cleaned up")
    )

    await strategy._submit_protective_stop_intent("entry_protective_stop")

    strategy._submit_protective_stop_without_cancelling_pipeline.assert_awaited_once()
    strategy._mark_unsubmitted_timeout_order_terminal.assert_not_awaited()
    assert strategy._last_stop_submit_sig == "TQQQ:50:47.04"
    assert strategy._stop_audit_started is True
    assert any(
        row.get("stage") == "protective_stop_recovery_armed"
        for row in strategy.ctx.engine.audit_writer.records
    )


@pytest.mark.asyncio
async def test_system_paused_blocks_submit_intent_with_timeout_before_pipeline():
    strategy = _make_strategy()
    strategy._system_paused = True
    envelope = qmod.DecisionEnvelope(
        decision_id="decision-system-paused",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=50.0,
        freshness=qmod.Freshness(ttl_ms=2000, dlb_ms=500),
        order_style=qmod.OrderStyle(kind="MKT"),
        extras={"order_ref": "paused-entry"},
    )

    with pytest.raises(RuntimeError, match="system_health_paused_submit_blocked:TQQQ:entry_submit_timeout_sec"):
        await strategy._submit_intent_with_timeout(envelope, timeout_key="entry_submit_timeout_sec")

    strategy.ctx.submit_intent.assert_not_awaited()
    assert any(
        row.get("stage") == "submit_intent_blocked_system_health"
        for row in strategy.ctx.engine.audit_writer.records
    )


@pytest.mark.asyncio
async def test_system_paused_protective_stop_reaches_uom_safety_lane():
    strategy = _make_strategy()
    strategy._system_paused = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._stop_px = 47.04
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=50.0))
    strategy._broker_position_qty = AsyncMock(return_value=50.0)

    await strategy._submit_protective_stop_intent("entry_protective_stop")

    strategy.ctx.submit_intent.assert_awaited_once()
    envelope = strategy.ctx.submit_intent.await_args.args[0]
    assert envelope.extras["protective_stop"] is True
    assert envelope.extras["close_only"] is True


@pytest.mark.asyncio
async def test_system_paused_flatten_reaches_uom_safety_lane():
    strategy = _make_strategy()
    strategy._system_paused = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"

    ok = await strategy._submit_flatten_intent_for_symbol("TQQQ", 50.0, "test_flatten")

    assert ok is True
    strategy.ctx.submit_intent.assert_awaited_once()
    envelope = strategy.ctx.submit_intent.await_args.args[0]
    assert envelope.extras["flatten_action"] is True
    assert envelope.extras["close_only"] is True


@pytest.mark.asyncio
async def test_system_paused_blocks_on_features_callback_path():
    strategy = _make_strategy()
    strategy._system_paused = True
    strategy.ctx.submit_intent = AsyncMock(side_effect=AssertionError("should not submit"))

    await strategy.on_features("QQQ", {"close": 100.0})

    strategy.ctx.submit_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_timeout_cleanup_marks_unsubmitted_new_order_rejected():
    strategy = _make_strategy()
    order_ref = "qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:test:1"
    pg = _FakePg(
        row={
            "order_id": "11111111-1111-1111-1111-111111111111",
            "note": json.dumps({"extras": {"order_ref": order_ref, "reason": "entry_protective_stop"}}),
        }
    )
    strategy.ctx.engine.pg = pg
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    envelope = qmod.DecisionEnvelope(
        decision_id="decision-timeout-cleanup",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-50.0,
        freshness=qmod.Freshness(ttl_ms=2000, dlb_ms=500),
        order_style=qmod.OrderStyle(kind="STP", stop_px=47.04),
        extras={"order_ref": order_ref, "trigger_id": "test", "protective_stop": True},
    )

    cleaned = await strategy._mark_unsubmitted_timeout_order_terminal(
        envelope,
        timeout_key="protective_stop_submit_timeout_sec",
        timeout_error="submit_intent_timeout:TQQQ:protective_stop_submit_timeout_sec:2.0",
    )

    assert cleaned is True
    assert len(pg.execute_calls) == 1
    sql_args = pg.execute_calls[0]
    assert "state = 'REJECTED'" in sql_args[0]
    assert sql_args[1] == order_ref
    updated_note = json.loads(sql_args[2])
    assert updated_note["timeout_cleanup"]["reason"] == "submit_intent_timeout_unsubmitted_order"


@pytest.mark.asyncio
async def test_submit_timeout_cleanup_skips_when_matching_live_order_visible():
    strategy = _make_strategy()
    order_ref = "qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:test:1"
    pg = _FakePg(row={"order_id": "11111111-1111-1111-1111-111111111111", "note": "{}"})
    strategy.ctx.engine.pg = pg
    live_order = MagicMock()
    live_order.order_ref = order_ref
    live_order.state = "SUBMITTED"
    live_order.order_id = 123
    strategy.ctx.open_orders = AsyncMock(return_value=[live_order])
    envelope = qmod.DecisionEnvelope(
        decision_id="decision-timeout-skip",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-50.0,
        freshness=qmod.Freshness(ttl_ms=2000, dlb_ms=500),
        order_style=qmod.OrderStyle(kind="STP", stop_px=47.04),
        extras={"order_ref": order_ref, "trigger_id": "test", "protective_stop": True},
    )

    cleaned = await strategy._mark_unsubmitted_timeout_order_terminal(
        envelope,
        timeout_key="protective_stop_submit_timeout_sec",
        timeout_error="submit_intent_timeout:TQQQ:protective_stop_submit_timeout_sec:2.0",
    )

    assert cleaned is False
    assert pg.fetchone_calls == []
    assert pg.execute_calls == []


@pytest.mark.asyncio
async def test_submit_timeout_cleanup_skips_when_open_orders_unavailable():
    strategy = _make_strategy()
    order_ref = "qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:test:1"
    pg = _FakePg(row={"order_id": "11111111-1111-1111-1111-111111111111", "note": "{}"})
    strategy.ctx.engine.pg = pg
    strategy.ctx.open_orders = AsyncMock(side_effect=RuntimeError("broker_open_orders_unavailable"))
    envelope = qmod.DecisionEnvelope(
        decision_id="decision-timeout-open-orders-fail",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        ts_bar_end_us=1,
        target_qty=-50.0,
        freshness=qmod.Freshness(ttl_ms=2000, dlb_ms=500),
        order_style=qmod.OrderStyle(kind="STP", stop_px=47.04),
        extras={"order_ref": order_ref, "trigger_id": "test", "protective_stop": True},
    )

    cleaned = await strategy._mark_unsubmitted_timeout_order_terminal(
        envelope,
        timeout_key="protective_stop_submit_timeout_sec",
        timeout_error="submit_intent_timeout:TQQQ:protective_stop_submit_timeout_sec:2.0",
    )

    assert cleaned is False
    assert pg.fetchone_calls == []
    assert pg.execute_calls == []


@pytest.mark.asyncio
async def test_resolve_broker_cancel_prefers_execution_broker():
    strategy = _make_strategy()
    exec_broker = MagicMock()
    exec_broker.cancel = AsyncMock(return_value=True)
    strategy.ctx.engine.execution_service = MagicMock()
    strategy.ctx.engine.execution_service.kernel = MagicMock()
    strategy.ctx.engine.execution_service.kernel.broker = exec_broker
    strategy.ctx.engine.broker_adapter = MagicMock(cancel=AsyncMock(return_value=True))
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock(cancel=AsyncMock(return_value=True))

    cancel_fn = strategy._resolve_broker_cancel_fn()
    assert cancel_fn is not None
    await cancel_fn(22)

    exec_broker.cancel.assert_awaited_once_with(22)
    strategy.ctx.engine.broker_adapter.cancel.assert_not_awaited()
    strategy.ctx.engine.market_data_service.ib_adapter.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_cleanup_uses_broker_open_orders_when_ctx_unavailable():
    strategy = _make_strategy()
    strategy.ctx.open_orders = AsyncMock(side_effect=RuntimeError("ctx_open_orders_unavailable"))
    order = OrderUpdate(
        order_id="22",
        strategy_id=strategy.strategy_id,
        symbol="SQQQ",
        state="SUBMITTED",
        order_type="STP",
        order_ref="qqqv1:qqq_intraday_001:SQQQ:STOP:TRG:test",
        note="protective_stop",
    )

    class _FakeExecBroker:
        def __init__(self, orders):
            self._orders = list(orders)
            self.cancel_calls = []

        async def open_orders(self):
            return list(self._orders)

        async def cancel(self, order_id: int):
            self.cancel_calls.append(int(order_id))
            return True

    exec_broker = _FakeExecBroker([order])
    strategy.ctx.engine.execution_service = MagicMock()
    strategy.ctx.engine.execution_service.kernel = MagicMock()
    strategy.ctx.engine.execution_service.kernel.broker = exec_broker

    cancelled = await strategy._cancel_strategy_open_orders_best_effort(
        reason="unit_test_cleanup",
        symbol="SQQQ",
    )

    assert cancelled == 1
    assert exec_broker.cancel_calls == [22]


@pytest.mark.asyncio
async def test_flatten_closes_short_inventory_with_buy_intent():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._refresh_ib_positions_best_effort = AsyncMock(return_value=False)
    strategy._current_exec_qty = AsyncMock(return_value=-137.0)
    strategy.ctx.submit_intent = AsyncMock()

    await strategy._flatten("unexpected_short_inventory")

    assert strategy.ctx.submit_intent.await_count >= 1
    envelope = strategy.ctx.submit_intent.await_args_list[0].args[0]
    assert envelope.target_qty == 137.0


@pytest.mark.asyncio
async def test_flatten_runs_post_cancel_sweep_after_submit():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy.params.extras["flatten_residual_retry_enable"] = False
    strategy.params.extras["flatten_post_cancel_enable"] = True
    strategy.params.extras["flatten_post_cancel_delay_sec"] = 0.0
    strategy._refresh_ib_positions_best_effort = AsyncMock(return_value=False)
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._submit_flatten_intent_for_symbol = AsyncMock(return_value=True)
    strategy._cancel_strategy_open_orders_best_effort = AsyncMock(return_value=0)

    await strategy._flatten("logic_retreat")

    assert strategy._cancel_strategy_open_orders_best_effort.await_count == 2
    first_call = strategy._cancel_strategy_open_orders_best_effort.await_args_list[0].kwargs
    second_call = strategy._cancel_strategy_open_orders_best_effort.await_args_list[1].kwargs
    assert first_call["reason"] == "flatten_pre_cancel:logic_retreat"
    assert first_call["symbol"] is None
    assert second_call["reason"] == "flatten_post_cancel:logic_retreat"
    assert second_call["symbol"] == "TQQQ"
    assert second_call["only_protective_stop"] is True


@pytest.mark.asyncio
async def test_post_cancel_cleanup_skips_non_stop_orders():
    strategy = _make_strategy()

    stop_order = OrderUpdate(
        order_id="21",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        state="SUBMITTED",
        order_type="STP",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:test",
        note="protective_stop",
    )
    flatten_order = OrderUpdate(
        order_id="22",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        state="SUBMITTED",
        order_type="MKT",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:FLATTEN:TRG:test",
        note="",
    )
    strategy._open_orders_for_protection_audit = AsyncMock(return_value=[stop_order, flatten_order])
    cancel_calls = []

    async def _cancel(order_id: int):
        cancel_calls.append(int(order_id))
        return True

    strategy._resolve_broker_cancel_fn = MagicMock(return_value=_cancel)

    cancelled = await strategy._cancel_strategy_open_orders_best_effort(
        reason="flatten_post_cancel:logic_retreat",
        symbol="TQQQ",
        only_protective_stop=True,
    )

    assert cancelled == 1
    assert cancel_calls == [21]


@pytest.mark.asyncio
async def test_sync_detects_short_inventory_and_triggers_flatten():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._success_count = 1
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._entry_px = 72.66
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(side_effect=lambda s: MagicMock(pos_qty=-137.0, avg_px=71.65) if s == "SQQQ" else None)
    strategy._flatten = AsyncMock()

    await strategy._sync_position_state(force=True)

    strategy._flatten.assert_awaited_once_with("unexpected_short_inventory")
    stages = [row["stage"] for row in strategy.ctx.engine.audit_writer.records]
    assert "position_sync_qty_resolution" in stages
    assert "unexpected_short_inventory_detected" in stages


@pytest.mark.asyncio
async def test_sync_short_inventory_is_throttled_within_interval():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._success_count = 1
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._entry_px = 72.66
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(side_effect=lambda s: MagicMock(pos_qty=-137.0, avg_px=71.65) if s == "SQQQ" else None)
    strategy._flatten = AsyncMock()
    strategy.params.extras["unexpected_short_action_interval_sec"] = 60.0

    await strategy._sync_position_state(force=True)
    await strategy._sync_position_state(force=True)

    strategy._flatten.assert_awaited_once_with("unexpected_short_inventory")


@pytest.mark.asyncio
async def test_sync_ctx_negative_broker_flat_reconciles_to_flat_without_short_loop():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 47.67
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy._flatten = AsyncMock()

    async def _pos(symbol):
        if symbol == "TQQQ":
            return MagicMock(pos_qty=-51.0, avg_px=47.67)
        return None

    async def _broker(symbol):
        if symbol == "TQQQ":
            return (0.0, None)
        if symbol == "SQQQ":
            return (0.0, None)
        return None

    strategy.ctx.position = AsyncMock(side_effect=_pos)
    strategy._broker_position_snapshot = AsyncMock(side_effect=_broker)

    await strategy._sync_position_state(force=True)

    strategy._flatten.assert_not_awaited()
    assert strategy._is_traded is False
    assert strategy._exec_symbol is None
    assert strategy._side is None


@pytest.mark.asyncio
async def test_dual_position_conflict_does_not_resubmit_when_already_active():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.state["is_flattening"] = True
    strategy.ctx.position = AsyncMock(
        side_effect=lambda s: MagicMock(pos_qty=100.0, avg_px=50.0) if s == "TQQQ" else MagicMock(pos_qty=120.0, avg_px=75.0)
    )
    strategy._broker_position_snapshot = AsyncMock(return_value=None)
    strategy._cancel_strategy_open_orders_best_effort = AsyncMock(return_value=0)
    strategy._submit_flatten_intent_for_symbol = AsyncMock(return_value=True)

    await strategy._sync_position_state(force=True)
    await strategy._sync_position_state(force=True)

    strategy._cancel_strategy_open_orders_best_effort.assert_awaited_once()
    assert strategy._submit_flatten_intent_for_symbol.await_count == 2


@pytest.mark.asyncio
async def test_dual_position_conflict_is_throttled_within_cooldown():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(
        side_effect=lambda s: MagicMock(pos_qty=90.0, avg_px=50.0) if s == "TQQQ" else MagicMock(pos_qty=95.0, avg_px=75.0)
    )
    strategy._broker_position_snapshot = AsyncMock(return_value=None)
    strategy._cancel_strategy_open_orders_best_effort = AsyncMock(return_value=0)
    strategy._submit_flatten_intent_for_symbol = AsyncMock(return_value=True)

    await strategy._sync_position_state(force=True)
    await strategy._sync_position_state(force=True)

    strategy._cancel_strategy_open_orders_best_effort.assert_awaited_once()
    assert strategy._submit_flatten_intent_for_symbol.await_count == 2


@pytest.mark.asyncio
async def test_dual_position_conflict_can_rearm_after_resolved():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy._broker_position_snapshot = AsyncMock(return_value=None)
    strategy._cancel_strategy_open_orders_best_effort = AsyncMock(return_value=0)
    strategy._submit_flatten_intent_for_symbol = AsyncMock(return_value=True)

    strategy.ctx.position = AsyncMock(
        side_effect=lambda s: MagicMock(pos_qty=90.0, avg_px=50.0) if s == "TQQQ" else MagicMock(pos_qty=95.0, avg_px=75.0)
    )
    await strategy._sync_position_state(force=True)
    assert strategy._dual_conflict_active is True
    strategy._cancel_strategy_open_orders_best_effort.assert_awaited_once()
    assert strategy._submit_flatten_intent_for_symbol.await_count == 2

    strategy.ctx.position = AsyncMock(
        side_effect=lambda s: MagicMock(pos_qty=90.0, avg_px=50.0) if s == "TQQQ" else MagicMock(pos_qty=0.0, avg_px=75.0)
    )
    await strategy._sync_position_state(force=True)
    assert strategy._dual_conflict_active is True
    await strategy._sync_position_state(force=True)
    assert strategy._dual_conflict_active is False

    strategy._last_dual_conflict_action_mono = 0.0
    strategy.ctx.position = AsyncMock(
        side_effect=lambda s: MagicMock(pos_qty=88.0, avg_px=50.0) if s == "TQQQ" else MagicMock(pos_qty=92.0, avg_px=75.0)
    )
    await strategy._sync_position_state(force=True)
    assert strategy._dual_conflict_active is True
    assert strategy._cancel_strategy_open_orders_best_effort.await_count == 2
    assert strategy._submit_flatten_intent_for_symbol.await_count == 4


@pytest.mark.asyncio
async def test_dual_position_conflict_retries_after_failed_flatten_submit():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(
        side_effect=lambda s: MagicMock(pos_qty=100.0, avg_px=50.0) if s == "TQQQ" else MagicMock(pos_qty=120.0, avg_px=75.0)
    )
    strategy._broker_position_snapshot = AsyncMock(return_value=None)
    strategy._cancel_strategy_open_orders_best_effort = AsyncMock(return_value=0)
    strategy._submit_flatten_intent_for_symbol = AsyncMock(side_effect=[RuntimeError("mock_fail"), True, True])

    await strategy._sync_position_state(force=True)
    assert strategy._dual_conflict_active is True
    strategy._cancel_strategy_open_orders_best_effort.assert_awaited_once()
    assert strategy._submit_flatten_intent_for_symbol.await_count == 1

    strategy._last_dual_conflict_action_mono = 0.0
    await strategy._sync_position_state(force=True)
    assert strategy._dual_conflict_active is True
    assert strategy._cancel_strategy_open_orders_best_effort.await_count == 2
    assert strategy._submit_flatten_intent_for_symbol.await_count == 3


@pytest.mark.asyncio
async def test_ensure_live_protection_skips_when_no_long_qty():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._entry_px = 71.65
    strategy._stop_px = 70.90
    strategy._last_protection_ensure_mono = 0.0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=-137.0))
    strategy._submit_protective_stop_intent = AsyncMock()

    await strategy._ensure_live_protection(datetime.now(TEST_TZ))

    strategy._submit_protective_stop_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_protection_still_runs_when_entry_gate_is_system_blocked():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 48.75
    strategy._stop_px = 48.1
    strategy._last_protection_ensure_mono = 0.0
    strategy.params.extras["entry_require_decision_pipeline_healthy"] = True
    strategy.params.extras["entry_require_market_data_healthy"] = True
    strategy.ctx.engine.decision_pipeline = None
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter.is_connected.return_value = False
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=100.0))
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._submit_protective_stop_intent = AsyncMock()

    await strategy._ensure_live_protection(datetime.now(TEST_TZ))

    strategy._submit_protective_stop_intent.assert_awaited_once_with("position_reconcile_protection")


@pytest.mark.asyncio
async def test_protective_stop_recovers_when_flattening_flag_stale():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 49.1
    strategy._stop_px = 48.6
    strategy.state["is_flattening"] = True
    loop_now = asyncio.get_running_loop().time()
    strategy._flattening_started_mono = loop_now - 30.0
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=100.0))
    strategy.ctx.submit_intent = AsyncMock()

    await strategy._submit_protective_stop_intent("position_reconcile_protection")

    assert strategy.state.get("is_flattening") is False
    assert strategy._flattening_started_mono == 0.0
    strategy.ctx.submit_intent.assert_awaited_once()


@pytest.mark.asyncio
async def test_flatten_recovers_when_flattening_flag_stale():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy.state["is_flattening"] = True
    strategy._flattening_started_mono = asyncio.get_running_loop().time() - 30.0
    strategy._refresh_ib_positions_best_effort = AsyncMock(return_value=False)
    strategy._current_exec_qty = AsyncMock(return_value=120.0)
    strategy.ctx.submit_intent = AsyncMock()

    await strategy._flatten("logic_retreat")

    strategy.ctx.submit_intent.assert_awaited()


def test_restore_state_sets_success_count_when_is_traded_true(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    today = datetime.now(TEST_TZ).strftime("%Y-%m-%d")
    strategy._state_path.write_text(
        json.dumps({"day": today, "is_traded": True, "success_count": 0}, ensure_ascii=False),
        encoding="utf-8",
    )

    strategy._restore_runtime_state()
    assert strategy._success_count == 0


def test_persist_runtime_state_marks_event_driven_semantics(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._obs_path = tmp_path / "qqq_intraday_observability_qqq_intraday_001.json"

    strategy._persist_runtime_state(reason="unit_test")

    payload = json.loads(strategy._state_path.read_text(encoding="utf-8"))
    assert payload["updated_at_semantics"] == "event_driven_state_snapshot"
    assert payload["last_state_change_at"] == payload["updated_at"]
    assert payload["realtime_status_path"] == str(strategy._obs_path)
    assert payload["realtime_status_field"] == "ts_now_et"


class _FakeIBReqOk:
    def isConnected(self):
        return True

    async def reqPositionsAsync(self):
        return []


class _FakeIBReqTimeout:
    def isConnected(self):
        return True

    async def reqPositionsAsync(self):
        raise asyncio.TimeoutError()

    def positions(self):
        return []


class _FakeIBReqTimeoutWithSlowPositions:
    def isConnected(self):
        return True

    async def reqPositionsAsync(self):
        raise asyncio.TimeoutError()

    def positions(self):
        time_mod.sleep(0.35)
        return []


class _FakeIBSnapshotSlowPositions:
    def positions(self):
        time_mod.sleep(0.35)
        return []


@pytest.mark.asyncio
async def test_refresh_ib_positions_marks_hard_confirmed_on_success():
    strategy = _make_strategy()
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock(ib=_FakeIBReqOk())

    ok = await strategy._refresh_ib_positions_best_effort(force=True)

    assert ok is True
    assert strategy._broker_positions_ready is True
    assert strategy._broker_positions_hard_confirmed is True


@pytest.mark.asyncio
async def test_refresh_ib_positions_fallback_does_not_mark_hard_confirmed():
    strategy = _make_strategy()
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock(ib=_FakeIBReqTimeout())

    ok = await strategy._refresh_ib_positions_best_effort(force=True)

    assert ok is True
    assert strategy._broker_positions_ready is True
    assert strategy._broker_positions_hard_confirmed is False


@pytest.mark.asyncio
async def test_refresh_ib_positions_fallback_slow_positions_does_not_stall():
    strategy = _make_strategy()
    strategy.strategy_id = "qqq_intraday_shadow_001"
    strategy.params.extras["legacy_trader_parity_mode"] = False
    strategy.params.extras["broker_positions_refresh_timeout_sec"] = 0.05
    strategy.params.extras["broker_positions_snapshot_timeout_sec"] = 0.05
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock(ib=_FakeIBReqTimeoutWithSlowPositions())

    start = asyncio.get_running_loop().time()
    ok = await strategy._refresh_ib_positions_best_effort(force=True)
    elapsed = asyncio.get_running_loop().time() - start

    assert ok is False
    assert elapsed < 0.30


@pytest.mark.asyncio
async def test_broker_position_snapshot_slow_positions_returns_none_fast():
    strategy = _make_strategy()
    strategy.strategy_id = "qqq_intraday_shadow_001"
    strategy.params.extras["legacy_trader_parity_mode"] = False
    strategy.params.extras["broker_positions_snapshot_timeout_sec"] = 0.05
    strategy._refresh_ib_positions_best_effort = AsyncMock(return_value=True)
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock(
        ib=_FakeIBSnapshotSlowPositions(),
        account="DU_TEST",
    )

    start = asyncio.get_running_loop().time()
    snap = await strategy._broker_position_snapshot("TQQQ")
    elapsed = asyncio.get_running_loop().time() - start

    assert snap is None
    assert elapsed < 0.30


@pytest.mark.asyncio
async def test_startup_reconciled_persist_requires_hard_broker_confirmation(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = False
    strategy.ctx.open_orders = AsyncMock(return_value=[])

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is False
    strategy.ctx.open_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_reconciled_persist_writes_after_hard_confirmation(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.ctx.open_orders = AsyncMock(return_value=[])

    await strategy._maybe_persist_startup_reconciled_state()

    payload = json.loads(strategy._state_path.read_text(encoding="utf-8"))
    assert payload["reason"] == "startup_reconciled"
    assert strategy._startup_reconciled_persisted is True
    strategy.ctx.open_orders.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_reconciled_persist_skips_when_open_orders_unavailable(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.ctx.open_orders = AsyncMock(side_effect=RuntimeError("ib down"))

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is False
    assert strategy._startup_reconciled_persisted is False


@pytest.mark.asyncio
async def test_startup_reconciled_persist_falls_back_to_legacy_behavior_when_open_orders_timeout(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.params.extras["ctx_open_orders_timeout_sec"] = 0.01

    async def _slow_open_orders():
        await asyncio.sleep(0.3)
        return []

    strategy.ctx.open_orders = AsyncMock(side_effect=_slow_open_orders)

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is True
    assert strategy._startup_reconciled_persisted is True


@pytest.mark.asyncio
async def test_startup_reconciled_persist_skips_when_open_orders_timeout_in_non_legacy_mode(tmp_path):
    strategy = _make_strategy()
    strategy.strategy_id = "qqq_intraday_shadow_001"
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_shadow_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.params.extras["ctx_open_orders_timeout_sec"] = 0.01

    async def _slow_open_orders():
        await asyncio.sleep(0.3)
        return []

    strategy.ctx.open_orders = AsyncMock(side_effect=_slow_open_orders)

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is False
    assert strategy._startup_reconciled_persisted is False


@pytest.mark.asyncio
async def test_startup_reconciled_persist_ignores_stale_unsent_new_order(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.params.extras["startup_reconciled_stale_new_grace_sec"] = 60.0
    stale_created_at = datetime.now(TEST_TZ) - timedelta(minutes=10)
    stale_open = MagicMock(
        strategy_id="qqq_intraday_001",
        state="NEW",
        broker_order_id=None,
        submitted_at=None,
        created_at=stale_created_at,
    )
    strategy.ctx.open_orders = AsyncMock(return_value=[stale_open])

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is True
    assert strategy._startup_reconciled_persisted is True


@pytest.mark.asyncio
async def test_startup_reconciled_persist_ignores_stale_unsent_new_order_with_ts_only(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.params.extras["startup_reconciled_stale_new_grace_sec"] = 60.0
    stale_ts = datetime.now(TEST_TZ) - timedelta(minutes=10)
    stale_open = OrderUpdate(
        order_id="stale-orderupdate",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        state="NEW",
        order_type="MKT",
        broker_order_id=None,
        ts=stale_ts,
    )
    strategy.ctx.open_orders = AsyncMock(return_value=[stale_open])

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is True
    assert strategy._startup_reconciled_persisted is True


@pytest.mark.asyncio
async def test_startup_reconciled_persist_keeps_recent_unsent_new_order_active_with_ts_only(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.params.extras["startup_reconciled_stale_new_grace_sec"] = 60.0
    recent_ts = datetime.now(TEST_TZ) - timedelta(seconds=10)
    recent_open = OrderUpdate(
        order_id="recent-orderupdate",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        state="NEW",
        order_type="MKT",
        broker_order_id=None,
        ts=recent_ts,
    )
    strategy.ctx.open_orders = AsyncMock(return_value=[recent_open])

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is False
    assert strategy._startup_reconciled_persisted is False


@pytest.mark.asyncio
async def test_startup_reconciled_persist_requires_startup_ready(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = False
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.ctx.open_orders = AsyncMock(return_value=[])

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is False
    strategy.ctx.open_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_reconciled_persist_skips_when_active_strategy_orders_exist(tmp_path):
    strategy = _make_strategy()
    strategy._state_path = tmp_path / "qqq_intraday_qqq_intraday_001.json"
    strategy._startup_positions_ready = True
    strategy._broker_positions_ready = True
    strategy._broker_positions_hard_confirmed = True
    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="db-oid",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="Submitted",
                order_type="LMT",
                broker_order_id="88",
            )
        ]
    )

    await strategy._maybe_persist_startup_reconciled_state()

    assert strategy._state_path.exists() is False
    assert strategy._startup_reconciled_persisted is False


@pytest.mark.asyncio
async def test_sync_position_reconciled_flat_cancels_orphan_stop_orders():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._success_count = 1
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 49.02
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="uuid-db-id",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="PreSubmitted",
                order_type="STP",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:12",
                broker_order_id="12",
            )
        ]
    )
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter.cancel = AsyncMock(return_value=True)

    await strategy._sync_position_state(force=True)

    strategy.ctx.engine.market_data_service.ib_adapter.cancel.assert_awaited_once_with(12)
    assert strategy._exec_symbol is None
    assert strategy._side is None


@pytest.mark.asyncio
async def test_success_count_increments_only_after_round_reconciled_flat():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._success_count = 0
    strategy._round_live_confirmed = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 49.02
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._cancel_strategy_open_orders_best_effort = AsyncMock(return_value=0)

    await strategy._sync_position_state(force=True)

    assert strategy._success_count == 1
    assert strategy._is_traded is False
    assert strategy._round_live_confirmed is False


@pytest.mark.asyncio
async def test_sync_position_clears_stale_state_during_warmup_when_startup_ready():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 49.02
    strategy._entry_time_et = datetime.now(TEST_TZ) - timedelta(minutes=5)
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=30)
    strategy._startup_positions_ready = True
    strategy._round_live_confirmed = False
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._cancel_strategy_open_orders_best_effort = AsyncMock(return_value=0)

    await strategy._sync_position_state(force=True)

    assert strategy._is_traded is False
    assert strategy._exec_symbol is None
    assert strategy._side is None


@pytest.mark.asyncio
async def test_sync_position_flat_orphan_sweep_cancels_strategy_orders_when_not_traded():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = False
    strategy._exec_symbol = None
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=300)
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="db-oid",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="Submitted",
                order_type="STP",
                broker_order_id="56",
            )
        ]
    )
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter.cancel = AsyncMock(return_value=True)

    await strategy._sync_position_state(force=True)

    strategy.ctx.engine.market_data_service.ib_adapter.cancel.assert_awaited_once_with(56)


@pytest.mark.asyncio
async def test_target_hit_submits_tp1_partial_for_long():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 49.00
    strategy._entry_atr_used = 0.60
    strategy._locked_velocity_sign = +1
    strategy._locked_target = 1.0
    strategy._trade_target_px = 49.60
    strategy._flatten = AsyncMock()
    strategy._last_price = MagicMock(return_value=49.65)
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._submit_tp1_partial_exit = AsyncMock(return_value=True)

    await strategy._manage_position(datetime(2026, 3, 2, 10, 6, tzinfo=TEST_TZ))

    strategy._flatten.assert_not_awaited()
    strategy._submit_tp1_partial_exit.assert_awaited_once_with(50)
    assert strategy._tp1_hit is True


@pytest.mark.asyncio
async def test_target_hit_submits_tp1_partial_for_sqqq():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "SQQQ"
    strategy._side = "SHORT"
    strategy._entry_px = 70.00
    strategy._entry_atr_used = 0.40
    strategy._locked_velocity_sign = -1
    strategy._locked_target = 1.0
    strategy._trade_target_px = 70.40
    strategy._flatten = AsyncMock()
    strategy._last_price = MagicMock(return_value=70.45)
    strategy._current_exec_qty = AsyncMock(return_value=80.0)
    strategy._submit_tp1_partial_exit = AsyncMock(return_value=True)

    await strategy._manage_position(datetime(2026, 3, 2, 10, 7, tzinfo=TEST_TZ))

    strategy._flatten.assert_not_awaited()
    strategy._submit_tp1_partial_exit.assert_awaited_once_with(40)
    assert strategy._tp1_hit is True


@pytest.mark.asyncio
async def test_tp1_then_trailing_stop_flattens_remaining():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._entry_atr_used = 1.0
    strategy._tp1_hit = True
    strategy._trail_floor_px = 100.2
    strategy._trail_high_px = 101.0
    strategy._trade_target_px = 101.0
    strategy._last_price = MagicMock(return_value=100.45)
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._flatten = AsyncMock()

    await strategy._manage_position(datetime(2026, 3, 2, 10, 12, tzinfo=TEST_TZ))

    strategy._flatten.assert_awaited_once_with("tp1_then_trailing_stop")


@pytest.mark.asyncio
async def test_manage_position_uses_trade_target_px_not_amp_target():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 48.06
    strategy._entry_atr_used = 0.2768
    strategy._locked_target = 1.0
    strategy._trade_target_px = 48.34
    strategy._flatten = AsyncMock()
    strategy._last_price = MagicMock(return_value=48.03)
    strategy._current_exec_qty = AsyncMock(return_value=145.0)
    strategy._submit_tp1_partial_exit = AsyncMock(return_value=True)

    await strategy._manage_position(datetime(2026, 3, 13, 9, 45, 3, tzinfo=TEST_TZ))

    strategy._submit_tp1_partial_exit.assert_not_awaited()
    strategy._flatten.assert_not_awaited()
    assert strategy._tp1_hit is False


@pytest.mark.asyncio
async def test_hard_stop_is_disabled_before_3_minutes():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    now = datetime(2026, 3, 2, 10, 0, tzinfo=TEST_TZ)
    strategy._entry_time_et = now - timedelta(minutes=2, seconds=30)
    strategy._stop_px = 99.5
    strategy._tp1_hit = False
    strategy._trade_target_px = None
    strategy._last_price = MagicMock(return_value=99.0)
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._flatten = AsyncMock()

    await strategy._manage_position(now)

    strategy._flatten.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_blocked_until_amp_locked_after_0945():
    strategy = _make_strategy()
    strategy._amp_locked = False

    await strategy._maybe_enter(datetime(2026, 3, 13, 9, 45, 1, tzinfo=TEST_TZ))

    strategy.ctx.open_orders.assert_not_awaited()
    strategy.ctx.submit_intent.assert_not_called()


@pytest.mark.asyncio
async def test_hard_stop_triggers_after_3_minutes_before_tp1():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    now = datetime(2026, 3, 2, 10, 0, tzinfo=TEST_TZ)
    strategy._entry_time_et = now - timedelta(minutes=4)
    strategy._stop_px = 99.5
    strategy._tp1_hit = False
    strategy._trade_target_px = None
    strategy._last_price = MagicMock(return_value=99.0)
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._flatten = AsyncMock()

    await strategy._manage_position(now)

    strategy._flatten.assert_awaited_once_with("stop_hit")


@pytest.mark.asyncio
async def test_hard_stop_disabled_after_tp1():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._entry_time_et = datetime.now(TEST_TZ) - timedelta(minutes=10)
    strategy._stop_px = 99.5
    strategy._tp1_hit = True
    strategy._entry_atr_used = 0.0  # Skip trailing branch so this test isolates hard-stop behavior.
    strategy._trade_target_px = 101.0
    strategy._last_price = MagicMock(return_value=99.0)
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._flatten = AsyncMock()

    now = datetime(2026, 2, 26, 10, 30, 0, tzinfo=TEST_TZ)
    await strategy._manage_position(now)

    strategy._flatten.assert_not_awaited()


@pytest.mark.asyncio
async def test_manage_position_skips_outside_rth():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._stop_px = 99.5
    strategy._entry_time_et = datetime(2026, 3, 2, 8, 20, tzinfo=TEST_TZ)
    strategy._last_price = MagicMock(return_value=99.0)
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._flatten = AsyncMock()

    # Pre-market ET should not run stop/TP management.
    await strategy._manage_position(datetime(2026, 3, 2, 8, 31, tzinfo=TEST_TZ))

    strategy._flatten.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_1s_ignores_offhours_for_aggregates():
    strategy = _make_strategy()
    ts = int(datetime(2026, 3, 2, 8, 15, tzinfo=TEST_TZ).timestamp() * 1000)
    row = {"ts": ts, "close": 500.0, "volume": 1000}

    await strategy._ingest_1s("QQQ", row)

    assert strategy._last_seen_ts_ms.get("QQQ") == ts
    assert strategy._min_bar is None
    assert len(strategy._qqq_buffer) == 0


@pytest.mark.asyncio
async def test_ingest_1s_drops_large_rewind_without_mutating_state():
    strategy = _make_strategy()
    strategy._last_seen_ts_ms["QQQ"] = 1772723698000  # 2026-03-05 10:14:58 ET
    strategy._min_bar = qmod._OhlcvBar(
        ts_start=datetime(2026, 3, 5, 10, 14, 0, tzinfo=TEST_TZ),
        ts_end=datetime(2026, 3, 5, 10, 15, 0, tzinfo=TEST_TZ),
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=10.0,
    )
    row = {"ts": 1772722491000, "close": 99.0, "volume": 1.0}  # 2026-03-05 09:54:51 ET

    await strategy._ingest_1s("QQQ", row)

    assert strategy._last_seen_ts_ms["QQQ"] == 1772723698000
    assert strategy._min_bar is not None
    assert strategy._min_bar.close == 100.0


@pytest.mark.asyncio
async def test_ingest_1s_offhours_large_rewind_suppresses_warning(monkeypatch):
    strategy = _make_strategy()
    strategy._last_seen_ts_ms["QQQ"] = 1772723698000
    strategy._is_regular_session = lambda _now: False  # type: ignore[assignment]
    warn_mock = MagicMock()
    monkeypatch.setattr(qmod.logger, "warning", warn_mock)
    row = {"ts": 1772722491000, "close": 99.0, "volume": 1.0}

    await strategy._ingest_1s("QQQ", row, now_hint=datetime(2026, 3, 5, 20, 10, tzinfo=TEST_TZ))

    warn_mock.assert_not_called()
    assert strategy._last_seen_ts_ms["QQQ"] == 1772723698000


@pytest.mark.asyncio
async def test_ingest_1s_rth_large_rewind_keeps_warning(monkeypatch):
    strategy = _make_strategy()
    strategy._last_seen_ts_ms["QQQ"] = 1772723698000
    strategy._is_regular_session = lambda _now: True  # type: ignore[assignment]
    warn_mock = MagicMock()
    monkeypatch.setattr(qmod.logger, "warning", warn_mock)
    row = {"ts": 1772722491000, "close": 99.0, "volume": 1.0}

    await strategy._ingest_1s("QQQ", row, now_hint=datetime(2026, 3, 5, 10, 10, tzinfo=TEST_TZ))

    warn_mock.assert_called_once()
    assert warn_mock.call_args.args[0] == "qqq_ts_rewind_recovered"


@pytest.mark.asyncio
async def test_seed_warmup_retry_skips_outside_rth():
    strategy = _make_strategy()
    strategy._seed_ready = False
    strategy._warmup_entry_seed_data = AsyncMock()

    # Sunday night ET: should not retry seed warmup.
    await strategy._maybe_retry_seed_warmup(datetime(2026, 3, 1, 23, 51, tzinfo=TEST_TZ))

    strategy._warmup_entry_seed_data.assert_not_awaited()


def test_seed_lock_skips_outside_rth():
    strategy = _make_strategy()
    strategy._seed_ready = False
    strategy._last_seed_retry_minute = None

    # Sunday night ET: should not mutate retry minute / emit deferred lock path.
    strategy._maybe_lock_seed(datetime(2026, 3, 1, 23, 51, tzinfo=TEST_TZ))

    assert strategy._last_seed_retry_minute is None


@pytest.mark.asyncio
async def test_tp1_then_eod_flattens_when_not_stopped_by_trailing():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._entry_time_et = datetime.now(TEST_TZ) - timedelta(minutes=30)
    strategy._tp1_hit = True
    strategy._entry_atr_used = 1.0
    strategy._trail_high_px = 102.0
    strategy._trail_floor_px = 100.2
    strategy._trade_target_px = 101.0
    strategy._last_price = MagicMock(return_value=101.8)  # Above trailing stop.
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._flatten = AsyncMock()

    now = datetime(2026, 2, 26, 15, 59, 0, tzinfo=TEST_TZ)
    await strategy._manage_position(now)

    strategy._flatten.assert_awaited_once_with("tp1_then_eod")


@pytest.mark.asyncio
async def test_logic_retreat_uses_realtime_price_vs_vwap():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_time_et = datetime.now(TEST_TZ) - timedelta(minutes=20)
    strategy.params.extras["logic_retreat_min_hold_sec"] = 0.0
    strategy._vwap_pv = 1000.0
    strategy._vwap_v = 10.0  # VWAP = 100
    strategy._last_price = MagicMock(side_effect=lambda sym: 99.0 if sym == "QQQ" else 50.0)
    strategy._flatten = AsyncMock()

    await strategy._maybe_logic_retreat(datetime.now(TEST_TZ))

    strategy._flatten.assert_awaited_once_with("logic_retreat")


@pytest.mark.asyncio
async def test_logic_retreat_skips_when_qqq_is_stale():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_time_et = datetime.now(TEST_TZ) - timedelta(minutes=20)
    strategy.params.extras["logic_retreat_min_hold_sec"] = 0.0
    strategy.params.extras["logic_retreat_max_qqq_age_sec"] = 5.0
    strategy._vwap_pv = 1000.0
    strategy._vwap_v = 10.0  # VWAP = 100
    now = datetime.now(TEST_TZ)
    strategy._last_seen_ts_ms["QQQ"] = int((now - timedelta(seconds=120)).timestamp() * 1000)
    strategy._last_price = MagicMock(side_effect=lambda sym: 99.0 if sym == "QQQ" else 50.0)
    strategy._flatten = AsyncMock()

    await strategy._maybe_logic_retreat(now)

    strategy._flatten.assert_not_awaited()


@pytest.mark.asyncio
async def test_manage_position_checks_logic_retreat_each_loop_not_minute_gated():
    strategy = _make_strategy()
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._entry_time_et = datetime.now(TEST_TZ) - timedelta(minutes=30)
    strategy._tp1_hit = False
    strategy.params.extras["enable_hard_stop"] = False
    strategy.params.extras["logic_retreat_min_hold_sec"] = 0.0
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._last_price = MagicMock(return_value=100.0)
    strategy._maybe_logic_retreat = AsyncMock()
    strategy._maybe_add_position = AsyncMock()

    now = datetime(2026, 2, 26, 10, 30, 15, tzinfo=TEST_TZ)
    await strategy._manage_position(now)
    await strategy._manage_position(now)

    assert strategy._maybe_logic_retreat.await_count == 2


@pytest.mark.asyncio
async def test_on_order_update_ignores_untrusted_filled_without_broker_or_entry_markers():
    strategy = _make_strategy()
    strategy._pending_decision_id = "pending-1"
    strategy._pending_decision_set_at_et = datetime.now(TEST_TZ)
    strategy._is_traded = False

    update = OrderUpdate(
        order_id="oid-1",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        state="FILLED",
        broker_order_id=None,
        order_ref=None,
        note=None,
    )
    await strategy.on_order_update(update)

    assert strategy._is_traded is False
    assert strategy._pending_decision_id == "pending-1"


@pytest.mark.asyncio
async def test_on_order_update_accepts_entry_ref_even_without_broker_id():
    strategy = _make_strategy()
    strategy._pending_decision_id = "pending-2"
    strategy._pending_decision_set_at_et = datetime.now(TEST_TZ)
    strategy._is_traded = False

    update = OrderUpdate(
        order_id="oid-2",
        strategy_id=strategy.strategy_id,
        symbol="TQQQ",
        state="FILLED",
        broker_order_id=None,
        order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY",
        note='{"extras":{"reason":"qqq_intraday_entry"}}',
    )
    await strategy.on_order_update(update)

    assert strategy._is_traded is True
    assert strategy._pending_decision_id is None


@pytest.mark.asyncio
async def test_sync_position_entry_timeout_cancels_all_orders_and_clears_state():
    strategy = _make_strategy()
    strategy._running = True
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=600)
    strategy._pending_decision_id = "pending-timeout"
    strategy._pending_decision_set_mono = asyncio.get_running_loop().time() - 90.0
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy.state["is_flattening"] = True
    strategy.params.extras["entry_fill_timeout_sec"] = 60.0
    strategy.params.extras["entry_timeout_reentry_cooldown_sec"] = 120.0
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="oid-entry",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="Submitted",
                order_type="MKT",
                broker_order_id="101",
            ),
            OrderUpdate(
                order_id="oid-stop",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="PreSubmitted",
                order_type="STP",
                broker_order_id="102",
            ),
            OrderUpdate(
                order_id="oid-other",
                strategy_id="other_strategy",
                symbol="TQQQ",
                state="Submitted",
                order_type="STP",
                broker_order_id="999",
            ),
        ]
    )
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter.cancel = AsyncMock(return_value=True)

    before = asyncio.get_running_loop().time()
    await strategy._sync_position_state(force=True)

    cancelled_ids = [args.args[0] for args in strategy.ctx.engine.market_data_service.ib_adapter.cancel.await_args_list]
    assert sorted(cancelled_ids) == [101, 102]
    assert strategy._pending_decision_id is None
    assert strategy._is_traded is False
    assert strategy._exec_symbol is None
    assert strategy._side is None
    assert strategy.state.get("is_flattening") is False
    assert strategy._entry_timeout_block_until_mono > before


@pytest.mark.asyncio
async def test_sync_position_pending_probe_open_orders_timeout_does_not_stall():
    strategy = _make_strategy()
    strategy.strategy_id = "qqq_intraday_shadow_001"
    strategy._running = True
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=600)
    strategy._pending_decision_id = "pending-open-orders-timeout"
    strategy._pending_decision_set_mono = asyncio.get_running_loop().time() - 30.0
    strategy._pending_decision_last_probe_mono = 0.0
    strategy.params.extras["pending_decision_min_hold_sec"] = 1.0
    strategy.params.extras["pending_decision_probe_interval_sec"] = 0.0
    strategy.params.extras["ctx_open_orders_timeout_sec"] = 0.01
    strategy.ctx.position = AsyncMock(return_value=None)

    async def _slow_open_orders():
        await asyncio.sleep(0.3)
        return []

    strategy.ctx.open_orders = AsyncMock(side_effect=_slow_open_orders)

    start = asyncio.get_running_loop().time()
    await strategy._sync_position_state(force=True)
    elapsed = asyncio.get_running_loop().time() - start

    assert elapsed < 1.0
    assert strategy._pending_decision_id == "pending-open-orders-timeout"
    assert strategy._pending_decision_flat_probe_count == 0


def test_legacy_trader_parity_mode_defaults_only_for_primary_strategy():
    strategy = _make_strategy()
    assert strategy._legacy_trader_parity_mode(strategy.params.extras) is True

    strategy.strategy_id = "qqq_intraday_shadow_001"
    assert strategy._legacy_trader_parity_mode(strategy.params.extras) is False


@pytest.mark.asyncio
async def test_maybe_enter_blocked_by_entry_timeout_cooldown():
    strategy = _make_strategy()
    strategy._running = True
    strategy._entry_timeout_block_until_mono = asyncio.get_running_loop().time() + 60.0
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    now = datetime(2026, 2, 26, 10, 0, 0, tzinfo=TEST_TZ)

    await strategy._maybe_enter(now)

    strategy.ctx.open_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_timeout_cancels_only_current_trigger_orders_when_tagged():
    strategy = _make_strategy()
    strategy._running = True
    strategy._initialized_at_et = datetime.now(TEST_TZ) - timedelta(seconds=600)
    strategy._pending_decision_id = "pending-timeout"
    strategy._pending_decision_set_mono = asyncio.get_running_loop().time() - 90.0
    strategy._active_trigger_id = "aaaabbbbccccdddd"
    strategy.params.extras["entry_fill_timeout_sec"] = 60.0
    strategy.params.extras["entry_timeout_reentry_cooldown_sec"] = 120.0
    strategy.ctx.position = AsyncMock(return_value=None)
    strategy.ctx.open_orders = AsyncMock(
        return_value=[
            OrderUpdate(
                order_id="oid-entry-cur",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="Submitted",
                order_type="MKT",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:aaaabbbbccccdddd",
                broker_order_id="201",
            ),
            OrderUpdate(
                order_id="oid-stop-cur",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="PreSubmitted",
                order_type="STP",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:aaaabbbbccccdddd:2",
                broker_order_id="202",
            ),
            OrderUpdate(
                order_id="oid-stop-old",
                strategy_id=strategy.strategy_id,
                symbol="TQQQ",
                state="Submitted",
                order_type="STP",
                order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:1111222233334444:9",
                broker_order_id="299",
            ),
        ]
    )
    strategy.ctx.engine.market_data_service = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter = MagicMock()
    strategy.ctx.engine.market_data_service.ib_adapter.cancel = AsyncMock(return_value=True)

    await strategy._sync_position_state(force=True)

    cancelled_ids = [args.args[0] for args in strategy.ctx.engine.market_data_service.ib_adapter.cancel.await_args_list]
    assert sorted(cancelled_ids) == [201, 202]
    assert strategy._active_trigger_id is None


@pytest.mark.asyncio
async def test_submit_target_and_stop_include_same_trigger_id():
    strategy = _make_strategy()
    strategy._running = True
    strategy.ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "reasons": []})
    strategy._exec_symbol = "TQQQ"
    strategy._is_traded = True
    strategy._stop_px = 47.04
    strategy.ctx.position = AsyncMock(return_value=MagicMock(pos_qty=50.0))
    strategy._broker_position_qty = AsyncMock(return_value=50.0)

    await strategy._submit_target("TQQQ", 50, reason="qqq_intraday_entry", kind="ENTRY", lock_entry=True)
    assert strategy._active_trigger_id is not None
    entry_env = strategy.ctx.submit_intent.await_args_list[-1].args[0]
    trig = entry_env.extras.get("trigger_id")
    assert trig == strategy._active_trigger_id
    assert f":TRG:{trig.upper()}" in str(entry_env.extras.get("order_ref", "")).upper()

    await strategy._submit_protective_stop_intent("entry_protective_stop")
    stop_env = strategy.ctx.submit_intent.await_args_list[-1].args[0]
    assert stop_env.extras.get("trigger_id") == trig
    assert f":TRG:{trig.upper()}" in str(stop_env.extras.get("order_ref", "")).upper()
    assert float(stop_env.extras.get("position_qty_hint")) == pytest.approx(50.0)
    assert stop_env.extras.get("position_qty_hint_source") == "strategy_runtime"


@pytest.mark.asyncio
async def test_submit_target_rejected_clears_pending_and_active_trigger():
    strategy = _make_strategy()
    strategy._running = True
    strategy.ctx.submit_intent = AsyncMock(return_value={"status": "REJECTED", "reasons": ["insufficient_history"]})

    ok = await strategy._submit_target("TQQQ", 100, reason="qqq_intraday_entry", kind="ENTRY", lock_entry=True)

    assert ok is False
    assert strategy._pending_decision_id is None
    assert strategy._active_trigger_id is None
    assert strategy._entry_sync_grace_until_mono == 0.0
    assert strategy._expected_entry_qty == 0


@pytest.mark.asyncio
async def test_submit_target_approved_keeps_entry_locks_and_expected_qty():
    strategy = _make_strategy()
    strategy._running = True
    strategy.ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED", "reasons": []})

    ok = await strategy._submit_target("TQQQ", 100, reason="qqq_intraday_entry", kind="ENTRY", lock_entry=True)

    assert ok is True
    assert strategy._pending_decision_id is not None
    assert strategy._active_trigger_id is not None
    assert strategy._entry_sync_grace_until_mono > 0.0
    assert strategy._expected_entry_qty == 100


@pytest.mark.asyncio
async def test_submit_target_exception_rolls_back_pending_and_trigger():
    strategy = _make_strategy()
    strategy._running = True
    strategy.ctx.submit_intent = AsyncMock(side_effect=RuntimeError("pipeline_down"))

    ok = await strategy._submit_target("TQQQ", 100, reason="qqq_intraday_entry", kind="ENTRY", lock_entry=True)

    assert ok is False
    assert strategy._pending_decision_id is None
    assert strategy._active_trigger_id is None


@pytest.mark.asyncio
async def test_submit_flatten_intent_rejected_returns_false():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy.ctx.submit_intent = AsyncMock(return_value={"status": "REJECTED", "reasons": ["risk_blocked"]})

    ok = await strategy._submit_flatten_intent_for_symbol("TQQQ", 100.0, "logic_retreat")

    assert ok is False
    env = strategy.ctx.submit_intent.await_args_list[-1].args[0]
    assert float(env.extras.get("position_qty_hint")) == pytest.approx(100.0)
    assert env.extras.get("position_qty_hint_source") == "strategy_runtime"


@pytest.mark.asyncio
async def test_submit_flatten_intent_dedupes_same_signature_within_window():
    strategy = _make_strategy()
    strategy._running = True
    strategy.params.extras["flatten_intent_dedupe_sec"] = 60.0
    strategy.ctx.submit_intent = AsyncMock(return_value={"status": "APPROVED"})

    first_ok = await strategy._submit_flatten_intent_for_symbol("TQQQ", 100.0, "logic_retreat")
    second_ok = await strategy._submit_flatten_intent_for_symbol("TQQQ", 100.0, "logic_retreat_residual_retry")

    assert first_ok is True
    assert second_ok is False
    assert strategy.ctx.submit_intent.await_count == 1


@pytest.mark.asyncio
async def test_submit_target_deferred_treated_as_rejected_and_rolls_back():
    strategy = _make_strategy()
    strategy._running = True
    strategy.ctx.submit_intent = AsyncMock(return_value={"status": "DEFERRED", "reasons": ["symbol_cap_exhausted"]})

    ok = await strategy._submit_target("TQQQ", 100, reason="qqq_intraday_entry", kind="ENTRY", lock_entry=True)

    assert ok is False
    assert strategy._pending_decision_id is None
    assert strategy._active_trigger_id is None
    assert strategy._entry_sync_grace_until_mono == 0.0


def test_third_trade_positive_day_pnl_gate_defaults_on_and_can_be_disabled():
    strategy = _make_strategy()

    assert strategy._third_trade_requires_positive_day_pnl({}) is True
    assert strategy._third_trade_requires_positive_day_pnl({"third_trade_requires_positive_day_pnl": False}) is False


def test_hard_stop_atr_mult_prefers_canonical_param_over_legacy_stop_atr_mult():
    strategy = _make_strategy()

    extras = {"hard_stop_atr_mult": 0.75, "stop_atr_mult": 2.5}
    assert strategy._hard_stop_atr_mult(extras) == pytest.approx(0.75)
    assert strategy._hard_stop_atr_mult({"stop_atr_mult": 1.1}) == pytest.approx(1.1)
    assert strategy._hard_stop_atr_mult({}) == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_ensure_live_protection_applies_stop_min_abs_floor():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._entry_atr_used = None
    strategy._stop_px = None
    strategy._last_protection_ensure_mono = 0.0
    strategy.params.extras["hard_stop_atr_mult"] = 0.75
    strategy.params.extras["stop_min_abs"] = 0.5
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._effective_atr = MagicMock(return_value=0.2)
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._submit_protective_stop_intent = AsyncMock()
    strategy._has_live_protective_stop_order = AsyncMock(return_value=False)

    await strategy._ensure_live_protection(datetime.now(TEST_TZ))

    assert strategy._stop_dist_pre == pytest.approx(0.5)
    assert strategy._stop_px == pytest.approx(99.5)


@pytest.mark.asyncio
async def test_ensure_live_protection_rebuild_uses_canonical_hard_stop_atr_mult():
    strategy = _make_strategy()
    strategy._running = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._entry_px = 100.0
    strategy._entry_atr_used = None
    strategy._stop_px = None
    strategy._last_protection_ensure_mono = 0.0
    strategy.params.extras["hard_stop_atr_mult"] = 0.75
    strategy.params.extras["stop_atr_mult"] = 2.5
    strategy.params.extras["stop_min_abs"] = 0.5
    strategy._current_exec_qty = AsyncMock(return_value=100.0)
    strategy._effective_atr = MagicMock(return_value=1.0)
    strategy.ctx.open_orders = AsyncMock(return_value=[])
    strategy._submit_protective_stop_intent = AsyncMock()
    strategy._has_live_protective_stop_order = AsyncMock(return_value=False)

    await strategy._ensure_live_protection(datetime.now(TEST_TZ))

    assert strategy._stop_px == pytest.approx(99.25)
    assert strategy._stop_dist_pre == pytest.approx(0.75)
    strategy._submit_protective_stop_intent.assert_awaited_once_with("position_reconcile_protection")
