from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import types
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("clickhouse_connect", types.ModuleType("clickhouse_connect"))

from services.reconciliation.runtime_reconciler import RuntimeReconciler


@pytest.mark.asyncio
async def test_cleanup_ghosts_does_not_finalize_in_report_only_mode() -> None:
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    adapter = SimpleNamespace(get_open_orders=AsyncMock(return_value=[]))
    decision_finalizer = AsyncMock(return_value=None)
    reservation = SimpleNamespace(release_on_terminal=AsyncMock(return_value=(True, 100.0)))

    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        reservation_service=reservation,
        decision_finalizer=decision_finalizer,
        order_event_enabled=False,
    )

    ghost = {
        "order_id": "a0945c57-f70c-417c-afbb-005b04211376",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "state": "SUBMITTED",
        "updated_at": "2026-03-04T10:00:00Z",
        "idempotency_key": "idem:test",
        "broker_order_id": "18",
        "note": '{"trace":{"decision_id":"dec-123","trace_id":"tr-1","strategy_run_id":"run-1"}}',
    }

    await reconciler._cleanup_ghosts([ghost])

    decision_finalizer.assert_not_awaited()
    reservation.release_on_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_ghosts_flat_note_does_not_finalize_in_report_only_mode() -> None:
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    adapter = SimpleNamespace(get_open_orders=AsyncMock(return_value=[]))
    decision_finalizer = AsyncMock(return_value=None)

    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        decision_finalizer=decision_finalizer,
        order_event_enabled=False,
    )

    ghost = {
        "order_id": "a0945c57-f70c-417c-afbb-005b04211376",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "state": "SUBMITTED",
        "updated_at": "2026-03-04T10:00:00Z",
        "idempotency_key": "idem:test",
        "broker_order_id": "18",
        "note": '{"decision_id":"dec-flat-1"}',
    }

    await reconciler._cleanup_ghosts([ghost])

    decision_finalizer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_ghosts_invalid_note_does_not_finalize_in_report_only_mode() -> None:
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")

    async def _fetchone(sql: str, *params):
        if "FROM core.core_order_event" in sql:
            return {"decision_id": "dec-from-event"}
        return None

    pg.fetchone = AsyncMock(side_effect=_fetchone)
    adapter = SimpleNamespace(get_open_orders=AsyncMock(return_value=[]))
    decision_finalizer = AsyncMock(return_value=None)

    reconciler = RuntimeReconciler(
        pg=pg,
        ib_adapter=adapter,
        decision_finalizer=decision_finalizer,
        order_event_enabled=False,
    )

    ghost = {
        "order_id": "a0945c57-f70c-417c-afbb-005b04211376",
        "strategy_id": "qqq_intraday_001",
        "symbol": "TQQQ",
        "state": "SUBMITTED",
        "updated_at": "2026-03-04T10:00:00Z",
        "idempotency_key": "idem:test",
        "broker_order_id": "18",
        "note": "not-json",
    }

    await reconciler._cleanup_ghosts([ghost])

    decision_finalizer.assert_not_awaited()


@pytest.mark.asyncio
async def test_has_db_working_orders_true_when_submitted_rows_exist() -> None:
    pg = MagicMock()
    pg.fetchrow = AsyncMock(return_value={"working_count": 1})
    reconciler = RuntimeReconciler(pg=pg, ib_adapter=SimpleNamespace(), order_event_enabled=False)

    assert await reconciler._has_db_working_orders() is True
