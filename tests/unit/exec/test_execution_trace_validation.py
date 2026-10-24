import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.execution_service import ExecutionService


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _result_without_trace():
    plan = SimpleNamespace(
        order_type="MKT",
        side="BUY",
        qty=1,
        limit_px=None,
        stop_px=None,
        tif="DAY",
    )
    return SimpleNamespace(
        status="APPROVED",
        decision_id="d_trace_1",
        strategy_id="s_trace_1",
        symbol="TQQQ",
        plan=plan,
        metadata={"envelope_extras": {}},
    )


@pytest.mark.anyio
async def test_trace_validation_non_blocking_allows_execution_path():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    pg.fetchone = AsyncMock(return_value=None)
    pg.fetchall = AsyncMock(return_value=[])

    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=False, reason="blocked", hits=[]))

    kernel = MagicMock()

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=False,
        trace_validation_enabled=True,
        trace_validation_blocking=False,
    )

    out = await svc.execute(_result_without_trace())
    # Reaches risk stage (not blocked by trace gate)
    assert out.stage == "risk"


@pytest.mark.anyio
async def test_trace_validation_blocking_stops_execution_early():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")

    risk = MagicMock()
    risk.approve = AsyncMock(return_value=SimpleNamespace(approved=True, reason="", hits=[]))

    kernel = MagicMock()

    svc = ExecutionService(
        pg=pg,
        risk_service=risk,
        safety_kernel=kernel,
        enable_execution=False,
        trace_validation_enabled=True,
        trace_validation_blocking=True,
    )

    out = await svc.execute(_result_without_trace())
    assert out.success is False
    assert out.stage == "validation"
    risk.approve.assert_not_awaited()
