from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.exec.execution_service as exec_mod
from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.execution_service import ExecutionService
from services.exec.models import PlacedOrder


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


@pytest.mark.asyncio
async def test_execution_service_emits_broker_ack_and_db_persisted_audit(monkeypatch):
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value={"order_id": "db-1", "broker_order_id": "201", "parent_order_id": None})

    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=True)

    kernel = MagicMock()
    kernel.place_entry_order = AsyncMock(return_value=_placed(201, "TQQQ", "BUY", "MKT", 5, order_ref="oref-201"))
    kernel.register_idempotency_key = MagicMock()
    kernel.broker = MagicMock()
    kernel.broker.position_qty = AsyncMock(return_value=0.0)
    kernel.broker.open_orders = AsyncMock(return_value=[])

    audit_writer = MagicMock()
    monkeypatch.setattr(exec_mod, "emit_order_event", AsyncMock(return_value=True))

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        order_event_enabled=False,
        audit_writer=audit_writer,
        flip_guard_enabled=False,
    )

    result = PipelineResult(
        status="APPROVED",
        decision_id="d_local_audit_1",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        plan=ExecPlan(
            order_type="MKT",
            side="BUY",
            qty=5,
            limit_px=None,
            stop_px=None,
            tif="DAY",
            price_collar_pct=3.0,
            oco={},
            child={"type": "NONE"},
            notes={},
        ),
        metadata={"envelope_extras": {"trace_id": "trace-local-audit-1", "trigger_id": "trg-local-audit-1"}},
    )

    out = await svc.execute(result)
    await svc.shutdown()

    assert out.success is True
    stages = [call.kwargs.get("stage") for call in audit_writer.emit.call_args_list]
    assert "broker_submit_requested" in stages
    assert "broker_submit_ack" in stages
    assert "db_persisted" in stages
