from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

from services.exec.execution_service import ExecutionService
from services.decision.translator import ExecPlan


def test_execution_service_injects_runtime_identity(monkeypatch):
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=False)
    monkeypatch.setattr(
        ExecutionService,
        "_current_ib_order_identity",
        staticmethod(lambda: {"ib_client_id": 207, "ib_session_role": "exec", "ib_connection_epoch": 9}),
    )

    extras = svc._inject_runtime_identity({})

    assert extras["origin_ib_client_id"] == 207
    assert extras["origin_ib_session_role"] == "exec"
    assert extras["origin_ib_epoch"] == 9


def test_execution_service_rejects_mismatched_runtime_identity(monkeypatch):
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=MagicMock(), enable_execution=False)
    monkeypatch.setattr(
        ExecutionService,
        "_current_ib_order_identity",
        staticmethod(lambda: {"ib_client_id": 207, "ib_session_role": "exec", "ib_connection_epoch": 9}),
    )

    ok, reason, meta = svc._validate_runtime_identity({"origin_ib_epoch": 8})

    assert ok is False
    assert reason == "origin_ib_epoch_mismatch"
    assert meta["current_ib_epoch"] == 9


@pytest.mark.asyncio
async def test_execution_service_passes_expected_epoch_to_kernel(monkeypatch):
    kernel = MagicMock()
    kernel.place_entry_order = AsyncMock()
    kernel.place_entry_order.return_value = SimpleNamespace(order_id=1, perm_id=11, broker_order_id="1")
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=kernel, enable_execution=False)
    monkeypatch.setattr(
        ExecutionService,
        "_current_ib_order_identity",
        staticmethod(lambda: {"ib_client_id": 207, "ib_session_role": "exec", "ib_connection_epoch": 9}),
    )

    plan = ExecPlan(
        order_type="MKT",
        side="BUY",
        qty=1.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=0.0,
        oco={},
        child={},
        notes={},
    )
    await svc._place_order(
        strategy_id="s1",
        symbol="QQQ",
        plan=plan,
        envelope_extras={"origin_ib_epoch": 9},
    )

    assert kernel.place_entry_order.await_args.kwargs["expected_epoch"] == 9


@pytest.mark.asyncio
async def test_execution_service_routes_protective_replacement_to_kernel(monkeypatch):
    kernel = MagicMock()
    kernel.place_order = AsyncMock()
    kernel.replace_protective_order_atomicish = AsyncMock(
        return_value=SimpleNamespace(order_id=2, perm_id=22, broker_order_id="2")
    )
    svc = ExecutionService(pg=MagicMock(), risk_service=MagicMock(), safety_kernel=kernel, enable_execution=False)
    monkeypatch.setattr(
        ExecutionService,
        "_current_ib_order_identity",
        staticmethod(lambda: {"ib_client_id": 207, "ib_session_role": "exec", "ib_connection_epoch": 9}),
    )
    plan = ExecPlan(
        order_type="TRAIL",
        side="SELL",
        qty=2.0,
        limit_px=None,
        stop_px=None,
        tif="DAY",
        price_collar_pct=0.0,
        oco={},
        child={},
        notes={},
        trail_percent=1.5,
        trail_stop_px=50.25,
    )

    await svc._place_order(
        strategy_id="s1",
        symbol="TQQQ",
        plan=plan,
        envelope_extras={
            "origin_ib_epoch": 9,
            "protective_stop": True,
            "close_only": True,
            "position_qty_hint": 2.0,
            "previous_stop_px": 48.5,
            "replace_protective_order_id": 55,
            "order_ref": "iswv2:s1:TQQQ:TRAIL:TRG:x:1",
        },
    )

    kernel.replace_protective_order_atomicish.assert_awaited_once()
    kernel.place_order.assert_not_awaited()
    kwargs = kernel.replace_protective_order_atomicish.await_args.kwargs
    assert kwargs["old_order_id"] == 55
    assert kwargs["order_type"] == "TRAIL"
    assert kwargs["trail_percent"] == 1.5
    assert kwargs["trail_stop_px"] == 50.25
    assert kwargs["previous_stop_px"] == 48.5
