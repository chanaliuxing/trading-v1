from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.models import Fill
from services.exec.safety_kernel import SafetyKernel


def _make_pg_mock():
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    pg.fetchone = AsyncMock(return_value=None)
    pg.fetchall = AsyncMock(return_value=[])
    return pg


def _make_broker_mock():
    broker = MagicMock()
    broker.place_market = AsyncMock()
    broker.place_limit = AsyncMock()
    broker.place_stop = AsyncMock()
    broker.place_stop_limit = AsyncMock()
    broker.cancel = AsyncMock(return_value=True)
    broker.open_orders = AsyncMock(return_value=[])
    return broker


@pytest.mark.asyncio
async def test_on_fill_finalizer_fallbacks_to_order_event_when_note_invalid() -> None:
    pg = _make_pg_mock()
    broker = _make_broker_mock()
    decision_finalizer = AsyncMock(return_value=None)
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=None,
        decision_finalizer=decision_finalizer,
    )

    async def _fetchone(sql: str, *params):
        if "WHERE order_ref = $1" in sql:
            return {
                "order_id": "00000000-0000-0000-0000-000000000101",
                "idempotency_key": "idem_101",
                "qty": 10.0,
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "side": "BUY",
                "note": "not-json",
            }
        if "SELECT COALESCE(SUM(f.qty)" in sql:
            return {"filled_qty": 10.0}
        if "FROM core.core_order_event" in sql:
            return {"decision_id": "dec-from-order-event"}
        return None

    pg.fetchone = AsyncMock(side_effect=_fetchone)

    fill = Fill(
        fill_id="ex:101",
        order_id=101,
        order_ref="any",
        symbol="TQQQ",
        qty=10.0,
        price=100.0,
        ts_us=1,
        side="BUY",
        exec_id="ex-101",
    )
    await kernel.on_fill(fill)

    decision_finalizer.assert_awaited_once_with("dec-from-order-event", "FILLED")


@pytest.mark.asyncio
async def test_on_cancel_finalizer_fallbacks_to_order_event_when_note_invalid() -> None:
    pg = _make_pg_mock()
    broker = _make_broker_mock()
    decision_finalizer = AsyncMock(return_value=None)
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=None,
        decision_finalizer=decision_finalizer,
    )

    async def _fetchone(sql: str, *params):
        if "SELECT note" in sql and "WHERE broker_order_id = $1" in sql:
            return {"note": "not-json"}
        if "FROM core.core_order_event" in sql:
            return {"decision_id": "dec-cancel-from-event"}
        return None

    pg.fetchone = AsyncMock(side_effect=_fetchone)

    await kernel.on_cancel(202, reason="test-cancel")

    decision_finalizer.assert_awaited_once_with("dec-cancel-from-event", "CANCELLED")


@pytest.mark.asyncio
async def test_on_cancel_updates_only_latest_matching_order_and_preserves_note_fields() -> None:
    pg = _make_pg_mock()
    broker = _make_broker_mock()
    decision_finalizer = AsyncMock(return_value=None)
    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=None,
        decision_finalizer=decision_finalizer,
    )

    async def _fetchone(sql: str, *params):
        if "SELECT order_id::text AS order_id, note" in sql:
            return {
                "order_id": "917acb9a-9380-40db-9235-463c2e2da125",
                "note": '{"decision_id":"dec-1","extras":{"reason":"unexpected_short_inventory"},"trace":{"trace_id":"t-1"}}',
            }
        return None

    pg.fetchone = AsyncMock(side_effect=_fetchone)

    await kernel.on_cancel(32, reason="pre_flatten:retry")

    update_calls = [c for c in pg.execute.call_args_list if "UPDATE core.core_order" in str(c.args[0])]
    assert len(update_calls) == 1
    _, note_json, order_uuid = update_calls[0].args
    assert order_uuid == "917acb9a-9380-40db-9235-463c2e2da125"
    assert '"decision_id": "dec-1"' in note_json
    assert '"reason": "unexpected_short_inventory"' in note_json
    assert '"cancel_reason": "pre_flatten:retry"' in note_json
