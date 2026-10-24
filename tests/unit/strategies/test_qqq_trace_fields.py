import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.exec.execution_state_model import FlattenLifecycleState
from libs.strategy import StrategyContext, StrategyParams, OrderUpdate
from strategies.qqq_intraday_strategy import QQQIntradayStrategy


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class _DummyEngine:
    def __init__(self):
        self.last_envelope = None
        self.audit_writer = MagicMock()

    async def submit_intent(self, strategy_id, envelope):
        self.last_envelope = envelope
        return {"status": "APPROVED", "reasons": []}


@pytest.mark.asyncio
async def test_qqq_submit_target_injects_trace_fields():
    engine = _DummyEngine()
    ctx = StrategyContext(
        engine=engine,
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    strategy = QQQIntradayStrategy(ctx, params)

    # Avoid file writes in unit test.
    strategy._state_path = None
    strategy._seed_ready = True

    ok = await strategy._submit_target("TQQQ", 1, lock_entry=False)
    assert ok is True
    assert engine.last_envelope is not None

    extras = dict(engine.last_envelope.extras or {})
    assert extras.get("strategy_run_id") == ctx.strategy_run_id
    assert extras.get("trace_id") == engine.last_envelope.decision_id
    assert str(extras.get("trigger_id") or "").strip()
    assert extras.get("intent_source") == "strategy"
    stages = [call.kwargs.get("stage") for call in engine.audit_writer.emit.call_args_list]
    assert "entry_intent_requested" in stages
    assert "entry_intent_submitted" in stages


@pytest.mark.asyncio
async def test_qqq_on_order_update_emits_entry_confirmed_audit():
    engine = _DummyEngine()
    ctx = StrategyContext(
        engine=engine,
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    strategy = QQQIntradayStrategy(ctx, params)
    strategy._state_path = None
    strategy._pending_decision_id = "dec-1"
    strategy._pending_decision_set_mono = asyncio.get_running_loop().time()
    strategy._active_trigger_id = "trg-1"

    update = OrderUpdate(
        order_id="1",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        state="FILLED",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:trg-1",
        broker_order_id="123",
    )

    await strategy.on_order_update(update)

    stages = [call.kwargs.get("stage") for call in engine.audit_writer.emit.call_args_list]
    assert "entry_confirmed" in stages


@pytest.mark.asyncio
async def test_qqq_runtime_lifecycle_transition_emits_state_domain_for_submit_and_fill():
    engine = _DummyEngine()
    ctx = StrategyContext(
        engine=engine,
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    strategy = QQQIntradayStrategy(ctx, params)
    strategy._state_path = None
    strategy._seed_ready = True

    ok = await strategy._submit_target("TQQQ", 1, lock_entry=True)
    assert ok is True

    strategy._active_trigger_id = "trg1"
    update = OrderUpdate(
        order_id="1",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        state="FILLED",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:trg1",
        broker_order_id="123",
    )
    await strategy.on_order_update(update)

    state_rows = [call.kwargs for call in engine.audit_writer.emit.call_args_list if call.kwargs.get("domain") == "state"]
    runtime_rows = [row for row in state_rows if row.get("stage") == "runtime_transition"]
    assert [row.get("status") for row in runtime_rows] == ["intent_submitted", "live"]
    assert runtime_rows[0]["payload"]["before_state"] == "flat"
    assert runtime_rows[0]["payload"]["after_state"] == "intent_submitted"
    assert runtime_rows[1]["payload"]["before_state"] == "intent_submitted"
    assert runtime_rows[1]["payload"]["after_state"] == "live"


@pytest.mark.asyncio
async def test_qqq_flatten_lifecycle_and_order_transitions_emit_state_domain():
    engine = _DummyEngine()
    ctx = StrategyContext(
        engine=engine,
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    strategy = QQQIntradayStrategy(ctx, params)
    strategy._state_path = None
    strategy._seed_ready = True
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy.state["is_flattening"] = True

    ok = await strategy._submit_flatten_intent_for_symbol("TQQQ", 10.0, "target_hit")
    assert ok is True

    strategy.state["is_flattening"] = False
    strategy._emit_flatten_lifecycle_transition(
        after_state=FlattenLifecycleState.CONFIRMED,
        reason="position_reconciled_flat",
        symbol="TQQQ",
    )

    state_rows = [call.kwargs for call in engine.audit_writer.emit.call_args_list if call.kwargs.get("domain") == "state"]
    stages = [row.get("stage") for row in state_rows]
    assert "flatten_transition" in stages
    assert "order_transition" in stages
    flatten_rows = [row for row in state_rows if row.get("stage") == "flatten_transition"]
    order_rows = [row for row in state_rows if row.get("stage") == "order_transition"]
    assert [row.get("status") for row in flatten_rows] == ["submitted", "confirmed"]
    assert order_rows[-1]["status"] == "submitted"


@pytest.mark.asyncio
async def test_qqq_protection_lifecycle_transition_emits_protected_on_stop_submit():
    engine = _DummyEngine()
    ctx = StrategyContext(
        engine=engine,
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    strategy = QQQIntradayStrategy(ctx, params)
    strategy._state_path = None
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._stop_px = 99.5
    strategy._active_trigger_id = "trg-stop"
    strategy._current_exec_qty = AsyncMock(return_value=10.0)

    await strategy._submit_protective_stop_intent("entry_protective_stop")

    state_rows = [call.kwargs for call in engine.audit_writer.emit.call_args_list if call.kwargs.get("domain") == "state"]
    protection_rows = [row for row in state_rows if row.get("stage") == "protection_transition"]
    assert protection_rows[-1]["status"] == "protected"
    assert protection_rows[-1]["reason"] == "entry_protective_stop"


@pytest.mark.asyncio
async def test_qqq_protection_lifecycle_transition_emits_failed_on_stop_reject():
    engine = _DummyEngine()

    async def _reject_submit(_strategy_id, envelope):
        engine.last_envelope = envelope
        return {"status": "REJECTED", "reasons": ["broker_rejected"]}

    engine.submit_intent = _reject_submit
    ctx = StrategyContext(
        engine=engine,
        strategy_id="qqq_intraday_001",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    strategy = QQQIntradayStrategy(ctx, params)
    strategy._state_path = None
    strategy._is_traded = True
    strategy._exec_symbol = "TQQQ"
    strategy._side = "LONG"
    strategy._stop_px = 99.5
    strategy._active_trigger_id = "trg-stop"
    strategy._current_exec_qty = AsyncMock(return_value=10.0)

    await strategy._submit_protective_stop_intent("audit_self_heal")

    state_rows = [call.kwargs for call in engine.audit_writer.emit.call_args_list if call.kwargs.get("domain") == "state"]
    protection_rows = [row for row in state_rows if row.get("stage") == "protection_transition"]
    assert protection_rows[-1]["status"] == "protection_failed"
    assert protection_rows[-1]["reason"] == "audit_self_heal"
