import json
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure project root is importable when pytest rootdir is `tests/`.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
import services.exec.execution_service as exec_mod
from services.exec.execution_service import ExecutionService
from services.exec.models import PlacedOrder, Fill
from services.exec.safety_kernel import SafetyKernel
import services.exec.execution_service as exec_mod


def _make_pg_mock():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value=None)
    return pg


def _make_risk_ok_mock():
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=True)
    return risk


def _make_broker_mock():
    broker = MagicMock()
    broker.place_market = AsyncMock()
    broker.place_limit = AsyncMock()
    broker.place_stop = AsyncMock()
    broker.place_stop_limit = AsyncMock()
    broker.cancel = AsyncMock(return_value=True)
    broker.open_orders = AsyncMock(return_value=[])
    # Default to a "safe" non-zero position for close-only guard in tests that don't care about positions.
    broker.position_qty = AsyncMock(return_value=10.0)
    return broker


def _placed(order_id: int, symbol: str, side: str, order_type: str, qty: float, *, order_ref: str = "ref") -> PlacedOrder:
    return PlacedOrder(
        order_id=order_id,
        broker_order_id=str(order_id),
        perm_id=order_id,
        order_ref=order_ref,
        symbol=symbol,
        side=side,
        order_type=order_type,
        qty=float(qty),
        limit_px=None,
        stop_px=None,
        tif="DAY",
        oca_group=None,
        status="SUBMITTED",
    )


def _owner_note(*, account_id: str = "DU_TEST", strategy_instance_id: str = "qqq_intraday_001") -> str:
    return json.dumps(
        {
            "extras": {
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "position_lot_id": "lot:qqq_intraday_001:TQQQ",
            }
        }
    )


@pytest.mark.asyncio
async def test_flatten_action_cancels_open_orders_and_uses_aux_placement():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "101",
                "order_type": "LMT",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "account_id": "DU_TEST",
                "strategy_instance_id": "qqq_intraday_001",
                "note": _owner_note(),
            },
            {
                "broker_order_id": "102",
                "order_type": "STP",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "account_id": "DU_TEST",
                "strategy_instance_id": "qqq_intraday_001",
                "note": _owner_note(),
            },
        ]
    )
    broker.open_orders = AsyncMock(
        return_value=[
            _placed(101, "TQQQ", "BUY", "LMT", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY"),
            _placed(102, "TQQQ", "SELL", "STP", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP"),
        ]
    )
    broker.place_market = AsyncMock(return_value=_placed(201, "TQQQ", "SELL", "MKT", 10, order_ref="flatten_ref"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("flatten must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_flatten",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={
            "envelope_extras": {
                "flatten_action": True,
                "reason": "test_flatten",
                "disable_oco": True,
                "account_id": "DU_TEST",
                "strategy_instance_id": "qqq_intraday_001",
            }
        },
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True

    broker.cancel.assert_any_await(101)
    broker.cancel.assert_any_await(102)
    assert broker.cancel.await_count == 2
    broker.place_market.assert_awaited()

    insert_calls = [c for c in pg.execute.call_args_list if "INSERT INTO core.core_order" in str(c.args[0])]
    assert insert_calls, "expected core_order insert"
    note_data = insert_calls[-1].args[-1]
    note_obj = json.loads(note_data)
    assert note_obj["extras"]["flatten_action"] is True
    assert exec_res.metadata.get("safety_action") == "flatten_action"
    await svc.shutdown()


@pytest.mark.asyncio
async def test_stock_close_only_uses_aux_placement_without_oco_plan():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        flip_guard_enabled=False,
    )

    broker.place_limit = AsyncMock(return_value=_placed(202, "NVDA", "SELL", "LMT", 1, order_ref="close_ref"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("stock close_only must not create an entry/OCO plan"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_stock_close_only",
        strategy_id="nvda_minute_flip_paper_001",
        symbol="NVDA",
        plan=ExecPlan(
            order_type="LMT",
            side="SELL",
            qty=1,
            limit_px=198.30,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"close_only": True, "position_qty_hint": 1.0, "order_ref": "close_ref"}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    broker.place_limit.assert_awaited()
    kernel.place_entry_order.assert_not_awaited()
    await svc.shutdown()


@pytest.mark.asyncio
async def test_flatten_order_submitted_event_contains_safety_action(monkeypatch):
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value={"order_id": "7bb27311-6a57-4cb0-af7a-2463a6b45f20", "broker_order_id": "201", "parent_order_id": None})
    broker.place_market = AsyncMock(return_value=_placed(201, "TQQQ", "SELL", "MKT", 10, order_ref="flatten_ref"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("flatten must not call place_entry_order"))

    emit_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(exec_mod, "emit_order_event", emit_mock)

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_flatten_event",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"flatten_action": True, "reason": "force_flatten_1559_EOD", "disable_oco": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    await svc.shutdown()

    submitted_events = [
        c.kwargs for c in emit_mock.await_args_list
        if c.kwargs.get("event_type") == "order_submitted"
    ]
    assert submitted_events, "expected order_submitted event"
    assert submitted_events[-1]["payload"]["safety_action"] == "flatten_action"


@pytest.mark.asyncio
async def test_protective_stop_cancels_only_existing_stop_orders():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)
    pg.fetchone = AsyncMock(return_value={"pos_qty": 10.0})

    pg.fetchall = AsyncMock(
        return_value=[
            {
                "broker_order_id": "301",
                "order_type": "STP",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "account_id": "DU_TEST",
                "strategy_instance_id": "qqq_intraday_001",
                "note": _owner_note(),
            },
            {
                "broker_order_id": "302",
                "order_type": "LMT",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "account_id": "DU_TEST",
                "strategy_instance_id": "qqq_intraday_001",
                "note": _owner_note(),
            },
            {
                "broker_order_id": "303",
                "order_type": "STP_LMT",
                "state": "SUBMITTED",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "account_id": "DU_TEST",
                "strategy_instance_id": "qqq_intraday_001",
                "note": _owner_note(),
            },
        ]
    )
    broker.open_orders = AsyncMock(
        return_value=[
            _placed(301, "TQQQ", "SELL", "STP", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP"),
            _placed(302, "TQQQ", "SELL", "LMT", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:TP"),
            _placed(303, "TQQQ", "SELL", "STP_LMT", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP"),
        ]
    )
    broker.place_stop = AsyncMock(return_value=_placed(401, "TQQQ", "SELL", "STP", 10, order_ref="sl_ref"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("protective_stop must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_stop",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="STP",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=99.0,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={
            "envelope_extras": {
                "protective_stop": True,
                "reason": "stop_update",
                "disable_oco": True,
                "account_id": "DU_TEST",
                "strategy_instance_id": "qqq_intraday_001",
            }
        },
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True

    broker.cancel.assert_any_await(301)
    broker.cancel.assert_any_await(303)
    assert 302 not in [c.args[0] for c in broker.cancel.await_args_list]
    registry_call = next(call for call in pg.execute.await_args_list if "core_protection_order_registry" in call.args[0])
    assert registry_call.args[1].startswith("UNKNOWN_ACCOUNT|qqq_intraday_001|TQQQ|lot:qqq_intraday_001:TQQQ|protective_stop|SELL|10|")
    assert registry_call.args[6] == "lot:qqq_intraday_001:TQQQ"
    assert registry_call.args[11] == "protective_stop"
    assert registry_call.args[15] == 10.0
    assert registry_call.args[16] == 99.0
    assert registry_call.args[17] == 401
    await svc.shutdown()


@pytest.mark.asyncio
async def test_broker_side_cancel_does_not_cancel_unknown_ghost_orders_missing_in_db():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    pg.fetchall = AsyncMock(return_value=[])
    broker.open_orders = AsyncMock(
        return_value=[
            _placed(9001, "TQQQ", "SELL", "STP", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:STOP"),
            _placed(9002, "TQQQ", "SELL", "STP", 10, order_ref="other_strategy:TQQQ:STOP"),
            _placed(9003, "AAPL", "SELL", "STP", 10, order_ref="qqqv1:qqq_intraday_001:AAPL:STOP"),
        ]
    )
    broker.place_market = AsyncMock(return_value=_placed(9100, "TQQQ", "SELL", "MKT", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:FLATTEN"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("flatten must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_flatten_ghost",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"flatten_action": True, "reason": "test_flatten", "disable_oco": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True

    broker.cancel.assert_not_awaited()
    assert 9002 not in [c.args[0] for c in broker.cancel.await_args_list]
    assert 9003 not in [c.args[0] for c in broker.cancel.await_args_list]
    await svc.shutdown()


@pytest.mark.asyncio
async def test_db_cancel_skips_when_broker_open_orders_unavailable():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    # DB says there are pending orders, but broker open-orders prefetch failed.
    # Service must not blindly cancel by old integer order IDs.
    pg.fetchall = AsyncMock(
        return_value=[
            {"broker_order_id": "101", "order_type": "STP", "state": "SUBMITTED"},
            {"broker_order_id": "102", "order_type": "LMT", "state": "SUBMITTED"},
        ]
    )
    broker.open_orders = AsyncMock(side_effect=RuntimeError("ib down"))
    broker.place_market = AsyncMock(return_value=_placed(1201, "TQQQ", "SELL", "MKT", 10, order_ref="qqqv1:qqq_intraday_001:TQQQ:FLATTEN"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("flatten must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_flatten_unverified_ids",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"flatten_action": True, "reason": "test_flatten", "disable_oco": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    assert broker.cancel.await_count == 0
    await svc.shutdown()


@pytest.mark.asyncio
async def test_close_only_guard_rejects_exit_when_flat():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    broker.position_qty = AsyncMock(return_value=0.0)
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    broker.place_stop = AsyncMock(side_effect=AssertionError("should not place stop when flat"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("protective_stop must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_stop_flat",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="STP",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=99.0,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"protective_stop": True, "reason": "stop_update", "disable_oco": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is False
    assert exec_res.metadata.get("safety_action") == "protective_stop"
    risk.release.assert_awaited()

    reject_updates = [c for c in pg.execute.call_args_list if "SET state = 'REJECTED'" in str(c.args[0])]
    assert reject_updates, "expected core_order rejected update"
    await svc.shutdown()


@pytest.mark.asyncio
async def test_close_only_guard_clamps_exit_qty_to_live_position():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    # Strategy-context position drives close-only sizing; broker qty is only a flatten fallback.
    pg.fetchone = AsyncMock(return_value={"pos_qty": 5.0})
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    broker.place_stop = AsyncMock(return_value=_placed(7001, "TQQQ", "SELL", "STP", 5, order_ref="sl_ref"))
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("protective_stop must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_stop_clamp",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="STP",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=99.0,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"protective_stop": True, "reason": "stop_update", "disable_oco": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    assert float(result.plan.qty) == 5.0
    assert broker.place_stop.await_args.args[2] == 5.0
    await svc.shutdown()


@pytest.mark.asyncio
async def test_flatten_action_market_fallback_when_primary_placement_fails():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    pg.fetchall = AsyncMock(return_value=[])
    # First call fails in primary path, second call succeeds in fallback path.
    kernel.place_order = AsyncMock(
        side_effect=[
            RuntimeError("transient broker submit failure"),
            _placed(888, "TQQQ", "SELL", "MKT", 10, order_ref="flatten_ref:FALLBACK_MKT"),
        ]
    )
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("flatten must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_flatten_fallback",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"flatten_action": True, "reason": "force_flatten_1559_EOD", "disable_oco": True, "order_ref": "flatten_ref"}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    assert exec_res.order_id == 888
    assert exec_res.metadata.get("safety_action") == "flatten_action"
    assert exec_res.metadata.get("fallback_used") is True
    assert kernel.place_order.await_count == 2


@pytest.mark.asyncio
async def test_protective_stop_market_fallback_when_stop_submit_fails():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)
    pg.fetchone = AsyncMock(return_value={"pos_qty": 10.0})
    pg.fetchall = AsyncMock(return_value=[])

    kernel.place_order = AsyncMock(
        side_effect=[
            RuntimeError("stop submit failure"),
            _placed(999, "TQQQ", "SELL", "MKT", 10, order_ref="stop_ref:PS_FALLBACK_MKT"),
        ]
    )
    kernel.place_entry_order = AsyncMock(side_effect=AssertionError("protective_stop must not call place_entry_order"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_stop_fallback",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="STP",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=99.0,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"protective_stop": True, "close_only": True, "reason": "stop_update", "disable_oco": True, "order_ref": "stop_ref"}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    assert exec_res.order_id == 999
    assert exec_res.metadata.get("safety_action") == "protective_stop"
    assert exec_res.metadata.get("fallback_used") is True
    assert kernel.place_order.await_count == 2


@pytest.mark.asyncio
async def test_safety_watchdog_warns_when_order_not_terminal(monkeypatch):
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        safety_terminal_watchdog_enabled=True,
        safety_terminal_watchdog_sec=0.0,
    )

    # close-only guard query then watchdog query.
    pg.fetchone = AsyncMock(side_effect=[
        {"pos_qty": 10.0},
        {"state": "SUBMITTED", "filled_qty": 0.0, "qty": 10.0, "submitted_at": None, "updated_at": None},
    ])
    pg.fetchall = AsyncMock(return_value=[])
    kernel.place_order = AsyncMock(return_value=_placed(1201, "TQQQ", "SELL", "MKT", 10, order_ref="flatten_ref"))
    warn_mock = MagicMock()
    monkeypatch.setattr(exec_mod.logger, "warning", warn_mock)

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_watchdog",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"flatten_action": True, "close_only": True, "disable_oco": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    await asyncio.sleep(0)
    await svc.shutdown()

    found = False
    for c in warn_mock.call_args_list:
        if c.args and c.args[0] == "safety_order_not_terminal_after_timeout":
            found = True
            break
    assert found, "expected watchdog timeout warning"


@pytest.mark.asyncio
async def test_safety_watchdog_auto_recovery_submits_market_order():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        safety_terminal_watchdog_enabled=True,
        safety_terminal_watchdog_sec=0.0,
        safety_watchdog_auto_recover_enabled=True,
    )

    pg.fetchone = AsyncMock(return_value={"state": "SUBMITTED", "side": "SELL", "filled_qty": 0.0, "qty": 10.0, "submitted_at": None, "updated_at": None})
    pg.fetchall = AsyncMock(return_value=[])
    broker.open_orders = AsyncMock(return_value=[])
    kernel.place_order = AsyncMock(side_effect=[
        _placed(1301, "TQQQ", "SELL", "MKT", 10, order_ref="flatten_ref"),
        _placed(1302, "TQQQ", "SELL", "MKT", 10, order_ref="WATCHDOG:flatten_action:idem"),
    ])

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_watchdog_recover",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"flatten_action": True, "close_only": True, "disable_oco": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    await asyncio.sleep(0.01)
    await svc.shutdown()
    assert kernel.place_order.await_count >= 2


@pytest.mark.asyncio
async def test_disable_oco_on_entry_passes_attach_oco_false():
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)
    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        flip_guard_enabled=False,
    )

    async def _place_entry(**kwargs):
        assert kwargs.get("attach_oco") is False
        return _placed(501, "TQQQ", "BUY", "MKT", 1, order_ref="entry_ref")

    kernel.place_entry_order = AsyncMock(side_effect=_place_entry)

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_entry",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=1,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"disable_oco": True, "reason": "entry"}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    await svc.shutdown()


@pytest.mark.asyncio
async def test_kernel_on_fill_updates_state_and_releases_reservation_without_plan():
    pg = _make_pg_mock()
    broker = _make_broker_mock()
    reservation = MagicMock()
    reservation.release_on_terminal = AsyncMock(return_value=(True, 0.0))
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=reservation)

    # Fill persistence + filled qty + idempotency lookup.
    async def _pg_fetchone(sql: str, *params):
        if "WHERE order_ref = $1" in sql:
            return {"order_id": "uuid_order_1", "idempotency_key": "idem_1", "qty": 10.0}
        if "SELECT COALESCE(SUM(f.qty)" in sql:
            return {"filled_qty": 10.0}
        return None

    pg.fetchone = AsyncMock(side_effect=_pg_fetchone)

    kernel.orders[555] = _placed(555, "TQQQ", "BUY", "MKT", 10, order_ref="any")
    fill = Fill(
        fill_id="ex:555",
        order_id=555,
        order_ref="any",
        symbol="TQQQ",
        qty=10.0,
        price=100.0,
        ts_us=1,
        exec_id="ex",
    )

    await kernel.on_fill(fill)
    reservation.release_on_terminal.assert_awaited()


@pytest.mark.asyncio
async def test_kernel_on_fill_uses_order_ref_resolution_when_broker_order_id_reused():
    pg = _make_pg_mock()
    broker = _make_broker_mock()
    reservation = MagicMock()
    reservation.release_on_terminal = AsyncMock(return_value=(True, 0.0))
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=reservation)

    async def _pg_fetchone(sql: str, *params):
        if "WHERE order_ref = $1" in sql:
            return {"order_id": "uuid_new_order", "idempotency_key": "idem_new", "qty": 133.0}
        if "SELECT COALESCE(SUM(f.qty)" in sql:
            assert params[0] == "uuid_new_order"
            return {"filled_qty": 133.0}
        return None

    pg.fetchone = AsyncMock(side_effect=_pg_fetchone)

    fill = Fill(
        fill_id="ex:4",
        order_id=4,
        order_ref="qqqv1:qqq_intraday_001:SQQQ:ENTRY",
        symbol="SQQQ",
        qty=133.0,
        price=75.18,
        ts_us=1,
        exec_id="ex-4",
    )

    await kernel.on_fill(fill)

    reservation.release_on_terminal.assert_awaited()
    release_kwargs = reservation.release_on_terminal.await_args.kwargs
    assert release_kwargs["idempotency_key"] == "idem_new"
    assert release_kwargs["order_id"] == "4"

    update_calls = [c for c in pg.execute.call_args_list if "UPDATE core.core_order" in str(c.args[0])]
    assert update_calls, "expected order state update"
    assert update_calls[-1].args[1] == "FILLED"
    assert update_calls[-1].args[2] == "uuid_new_order"
