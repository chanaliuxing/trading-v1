import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
from services.exec.models import PlacedOrder
import services.exec.netting.netted_execution_service as netted_mod
from services.exec.netting import NettedExecutionService
from services.exec.trace import TraceContext


def _mk_result(strategy_id: str, decision_id: str, symbol: str = "TQQQ") -> PipelineResult:
    return PipelineResult(
        status="APPROVED",
        decision_id=decision_id,
        strategy_id=strategy_id,
        symbol=symbol,
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
        metadata={"envelope_extras": {}},
    )


def _placed(order_id: int) -> PlacedOrder:
    return PlacedOrder(
        order_id=order_id,
        broker_order_id=str(order_id),
        perm_id=order_id,
        order_ref="nt:TQQQ:abcdef12",
        symbol="TQQQ",
        side="BUY",
        order_type="MKT",
        qty=20.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        oca_group=None,
        status="SUBMITTED",
    )


async def _run_dual_submit(service: NettedExecutionService, *, with_trace: bool = False):
    r1 = _mk_result("s1", "d1")
    r2 = _mk_result("s2", "d2")
    t1 = None
    t2 = None
    if with_trace:
        t1 = TraceContext(
            strategy_id="s1",
            strategy_run_id="run1",
            trigger_id="tr1",
            decision_id="d1",
            trace_id="trace1",
            causation_id="cause1",
        )
        t2 = TraceContext(
            strategy_id="s2",
            strategy_run_id="run2",
            trigger_id="tr2",
            decision_id="d2",
            trace_id="trace2",
            causation_id="cause2",
        )
    return await asyncio.gather(
        service.execute(r1, {}, trace_ctx=t1),
        service.execute(r2, {}, trace_ctx=t2),
    )


def test_netted_execution_batches_same_symbol_requests(anyio_backend):
    pg = MagicMock()
    # parent insert then child insert x2
    pg.fetchone = AsyncMock(side_effect=[
        {"order_id": "00000000-0000-0000-0000-000000000001"},
        {"order_id": "00000000-0000-0000-0000-000000000011"},
        {"order_id": "00000000-0000-0000-0000-000000000022"},
    ])
    pg.execute = AsyncMock(return_value="OK")

    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=True)

    kernel = MagicMock()
    kernel.place_entry_order = AsyncMock(return_value=_placed(101))
    kernel.register_idempotency_key = MagicMock()

    svc = NettedExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        batch_window_ms=10,
    )

    out1, out2 = asyncio.run(_run_dual_submit(svc, with_trace=True))

    assert out1.success is True
    assert out2.success is True
    assert out1.order_id == 101
    assert out2.order_id == 101
    assert kernel.place_entry_order.await_count == 1
    assert risk.approve.await_count == 2


def test_netted_emit_event_failure_does_not_block_other_futures(monkeypatch):
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=[
        {"order_id": "00000000-0000-0000-0000-000000000101"},
        {"order_id": "00000000-0000-0000-0000-000000000111"},
        {"order_id": "00000000-0000-0000-0000-000000000122"},
    ])
    pg.execute = AsyncMock(return_value="OK")

    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=True)

    kernel = MagicMock()
    kernel.place_entry_order = AsyncMock(return_value=_placed(202))
    kernel.register_idempotency_key = MagicMock()

    emit_mock = AsyncMock(side_effect=RuntimeError("order_event_db_down"))
    monkeypatch.setattr(netted_mod, "emit_order_event", emit_mock)

    svc = NettedExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        batch_window_ms=10,
    )

    out1, out2 = asyncio.run(_run_dual_submit(svc, with_trace=True))

    assert out1.success is True
    assert out2.success is True
    assert out1.order_id == 202
    assert out2.order_id == 202
    assert kernel.place_entry_order.await_count == 1
    assert emit_mock.await_count == 2


def test_netted_risk_envelope_includes_envelope_extras(anyio_backend):
    pg = MagicMock()
    pg.fetchone = AsyncMock(side_effect=[
        {"order_id": "00000000-0000-0000-0000-000000000201"},
        {"order_id": "00000000-0000-0000-0000-000000000211"},
    ])
    pg.execute = AsyncMock(return_value="OK")

    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))
    risk.release = AsyncMock(return_value=True)

    kernel = MagicMock()
    kernel.register_idempotency_key = MagicMock()

    svc = NettedExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=False,
        batch_window_ms=10,
    )

    r1 = _mk_result("s1", "d-extra")
    out = asyncio.run(svc.execute(r1, {"flatten_action": True, "close_only": True}))

    assert out.success is True
    assert out.stage == "dry_run"
    assert risk.approve.await_count == 1
    env = risk.approve.await_args.kwargs["envelope"]
    assert bool(env.get("extras", {}).get("flatten_action")) is True
    assert bool(env.get("extras", {}).get("close_only")) is True
