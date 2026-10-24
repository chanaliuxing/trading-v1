import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import types

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Optional dependency in local test env.
sys.modules.setdefault("clickhouse_connect", types.ModuleType("clickhouse_connect"))

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.exec.models import PlacedOrder
from services.exec.safety_kernel import SafetyKernel


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def execution_services():
    services = []
    yield services
    for svc in services:
        await svc.shutdown()


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


@pytest.mark.anyio
async def test_execution_publishes_notification_on_successful_order(execution_services):
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    notification_manager = MagicMock()
    notification_manager.publish = MagicMock(return_value=True)

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        flip_guard_enabled=False,
        notification_manager=notification_manager,
    )
    execution_services.append(svc)

    broker.place_market = AsyncMock(return_value=_placed(101, "TQQQ", "BUY", "MKT", 10, order_ref="qqq:test"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_notify_ok",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"reason": "qqq_intraday_entry"}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    notification_manager.publish.assert_called_once()

    event = notification_manager.publish.call_args.args[0]
    assert event.strategy_id == "qqq_intraday_001"
    assert event.symbol == "TQQQ"
    assert event.order_id == 101


@pytest.mark.anyio
async def test_execution_does_not_fail_when_notification_publish_raises(execution_services):
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    notification_manager = MagicMock()
    notification_manager.publish = MagicMock(side_effect=RuntimeError("notify error"))

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        flip_guard_enabled=False,
        notification_manager=notification_manager,
    )
    execution_services.append(svc)

    broker.place_market = AsyncMock(return_value=_placed(202, "TQQQ", "BUY", "MKT", 10, order_ref="qqq:test"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_notify_err",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"reason": "qqq_intraday_entry"}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True


@pytest.mark.anyio
async def test_execution_notification_falls_back_to_order_id_when_broker_order_id_missing(execution_services):
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    notification_manager = MagicMock()
    notification_manager.publish = MagicMock(return_value=True)

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        flip_guard_enabled=False,
        notification_manager=notification_manager,
    )
    execution_services.append(svc)

    broker.place_market = AsyncMock(
        return_value=PlacedOrder(
            order_id=303,
            broker_order_id=None,
            perm_id=303,
            order_ref="qqq:test:fallback",
            symbol="TQQQ",
            side="BUY",
            order_type="MKT",
            qty=10.0,
            status="SUBMITTED",
        )
    )

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_notify_fallback",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=10,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"reason": "qqq_intraday_entry"}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    event = notification_manager.publish.call_args.args[0]
    assert event.broker_order_id == "303"


@pytest.mark.anyio
async def test_execution_suppresses_audit_self_heal_protective_stop_notification(execution_services):
    pg = _make_pg_mock()
    risk = _make_risk_ok_mock()
    broker = _make_broker_mock()
    kernel = SafetyKernel(broker_adapter=broker, pg=pg, reservation_service=None)

    notification_manager = MagicMock()
    notification_manager.publish = MagicMock(return_value=True)

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        notification_manager=notification_manager,
    )
    execution_services.append(svc)

    broker.place_stop = AsyncMock(return_value=_placed(303, "TQQQ", "SELL", "STP", 10, order_ref="qqq:stop:audit"))

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_notify_audit_self_heal",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="STP",
            side="SELL",
            qty=10,
            limit_px=None,
            stop_px=45.0,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"reason": "audit_self_heal", "protective_stop": True}},
    )

    exec_res = await svc.execute(result)
    assert exec_res.success is True
    notification_manager.publish.assert_not_called()
