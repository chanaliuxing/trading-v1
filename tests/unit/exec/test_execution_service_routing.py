import sys
import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.decision.pipeline import PipelineResult
from services.decision.translator import ExecPlan
import services.exec.execution_service as exec_mod
from services.exec.execution_service import ExecutionService
from services.exec.netting import ExecutionRouter, RoutingConfig
from services.exec.trace import TraceContext


class _NoQuarantinePG:
    async def fetchone(self, *args, **kwargs):
        return None


def _result(mode: str) -> PipelineResult:
    return PipelineResult(
        status="APPROVED",
        decision_id="d1",
        strategy_id="s1",
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
        metadata={"envelope_extras": {"execution_mode": mode}},
    )


def test_execution_service_routes_to_netted_when_requested(anyio_backend):
    pg = _NoQuarantinePG()
    risk = MagicMock()
    kernel = MagicMock()

    router = ExecutionRouter(RoutingConfig(default_mode="direct"))
    netted = MagicMock()
    netted.execute = AsyncMock(return_value=MagicMock(
        success=True,
        decision_id="d1",
        order_id=777,
        idempotency_key="idem",
        reason="ok",
        stage="completed",
        metadata={"x": 1},
    ))

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        execution_router=router,
        netted_execution_service=netted,
    )

    out = __import__("asyncio").run(svc.execute(_result("netted")))

    assert out.success is True
    assert out.order_id == 777
    assert netted.execute.await_count == 1


def test_execution_service_keeps_direct_for_flatten(anyio_backend):
    pg = MagicMock()
    risk = MagicMock()
    risk.approve = AsyncMock(return_value=MagicMock(approved=False, reason="blocked", hits=[]))
    kernel = MagicMock()

    router = ExecutionRouter(RoutingConfig(default_mode="netted"))
    netted = MagicMock()
    netted.execute = AsyncMock()

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=True,
        execution_router=router,
        netted_execution_service=netted,
    )

    r = _result("netted")
    r.metadata["envelope_extras"]["flatten_action"] = True
    out = __import__("asyncio").run(svc.execute(r))

    assert out.success is False
    assert out.stage == "risk"
    assert netted.execute.await_count == 0


def test_emit_event_non_timeout_exception_is_non_blocking(monkeypatch):
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    emit_mock = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(exec_mod, "emit_order_event", emit_mock)

    trace = TraceContext(
        strategy_id="s1",
        strategy_run_id="run1",
        trigger_id="tr1",
        decision_id="d1",
        trace_id="trace1",
        causation_id="cause1",
    )

    async def _run_once():
        await svc._emit_event(
            event_type="order_submitted",
            trace_ctx=trace,
            symbol="TQQQ",
            intent_id="idem1",
            payload={"x": 1},
        )
        await asyncio.sleep(0.02)

    asyncio.run(_run_once())
    assert emit_mock.await_count == 1


def test_emit_event_overflow_is_buffered_and_drained(monkeypatch):
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)
    svc._event_emit_max_pending = 1

    async def _slow_emit(**kwargs):
        await asyncio.sleep(0.2)

    emit_mock = AsyncMock(side_effect=_slow_emit)
    monkeypatch.setattr(exec_mod, "emit_order_event", emit_mock)

    trace = TraceContext(
        strategy_id="s1",
        strategy_run_id="run1",
        trigger_id="tr1",
        decision_id="d1",
        trace_id="trace1",
        causation_id="cause1",
    )

    async def _run_twice():
        await svc._emit_event(event_type="intent_received", trace_ctx=trace, symbol="TQQQ", intent_id="i1")
        await svc._emit_event(event_type="risk_checked", trace_ctx=trace, symbol="TQQQ", intent_id="i2")
        await asyncio.sleep(0.45)

    asyncio.run(_run_twice())
    assert emit_mock.await_count == 2
    assert len(svc._event_emit_overflow) == 0


def test_emit_event_overflow_prefers_critical_events(monkeypatch):
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)
    svc._event_emit_max_pending = 0
    svc._event_emit_overflow = deque(maxlen=2)

    trace = TraceContext(
        strategy_id="s1",
        strategy_run_id="run1",
        trigger_id="tr1",
        decision_id="d1",
        trace_id="trace1",
        causation_id="cause1",
    )

    svc._schedule_event_emit({"event_type": "intent_received", "trace_ctx": trace, "symbol": "TQQQ"})
    svc._schedule_event_emit({"event_type": "risk_checked", "trace_ctx": trace, "symbol": "TQQQ"})
    svc._schedule_event_emit({"event_type": "order_submitted", "trace_ctx": trace, "symbol": "TQQQ"})

    types = [str(e.get("event_type")) for e in svc._event_emit_overflow]
    assert "order_submitted" in types


def test_shutdown_drains_event_tasks(monkeypatch):
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)

    gate = asyncio.Event()

    async def _slow_emit(**kwargs):
        await gate.wait()
        return True

    emit_mock = AsyncMock(side_effect=_slow_emit)
    monkeypatch.setattr(exec_mod, "emit_order_event", emit_mock)

    trace = TraceContext(
        strategy_id="s1",
        strategy_run_id="run1",
        trigger_id="tr1",
        decision_id="d1",
        trace_id="trace1",
        causation_id="cause1",
    )

    async def _run():
        await svc._emit_event(
            event_type="order_submitted",
            trace_ctx=trace,
            symbol="TQQQ",
            intent_id="idem1",
            payload={"x": 1},
        )
        gate.set()
        await svc.shutdown()
        assert len(svc._event_emit_tasks) == 0

    asyncio.run(_run())
    assert emit_mock.await_count == 1


def test_shutdown_does_not_drain_overflow_into_new_tasks(monkeypatch):
    pg = MagicMock()
    risk = MagicMock()
    kernel = MagicMock()
    svc = ExecutionService(pg=pg, risk_service=risk, safety_kernel=kernel, enable_execution=True)
    svc._event_emit_max_pending = 1

    gate = asyncio.Event()

    async def _slow_emit(**kwargs):
        await gate.wait()
        return True

    emit_mock = AsyncMock(side_effect=_slow_emit)
    monkeypatch.setattr(exec_mod, "emit_order_event", emit_mock)

    trace = TraceContext(
        strategy_id="s1",
        strategy_run_id="run1",
        trigger_id="tr1",
        decision_id="d1",
        trace_id="trace1",
        causation_id="cause1",
    )

    async def _run():
        await svc._emit_event(event_type="intent_received", trace_ctx=trace, symbol="TQQQ", intent_id="i1")
        await svc._emit_event(event_type="risk_checked", trace_ctx=trace, symbol="TQQQ", intent_id="i2")
        assert len(svc._event_emit_overflow) == 1
        gate.set()
        await svc.shutdown()
        assert len(svc._event_emit_tasks) == 0
        assert len(svc._event_emit_overflow) == 0

    asyncio.run(_run())
    assert emit_mock.await_count == 1
