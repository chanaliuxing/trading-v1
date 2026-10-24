from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.fill_event_handler import FillEventHandler
from services.exec.models import Fill


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _mk_trade(order_id: int = 4, perm_id: int | None = None, status: str = "Filled"):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, orderRef="qqqv1:qqq_intraday_001:TQQQ:FLATTEN", permId=perm_id, action="SELL", orderType="MKT"),
        contract=SimpleNamespace(symbol="TQQQ"),
        orderStatus=SimpleNamespace(status=status),
    )


def _mk_fill(exec_id: str = "e1", perm_id: int | None = None):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id,
            shares=2.0,
            price=50.0,
            exchange="SMART",
            permId=perm_id,
            side="SELL",
        ),
        commissionReport=SimpleNamespace(commission=0.1),
        time=SimpleNamespace(timestamp=lambda: 1_700_000_000.0),
    )


@pytest.mark.anyio
async def test_fill_event_publishes_notification():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()

    pg = MagicMock()
    pg.fetchone = AsyncMock(
        return_value={
            "strategy_id": "qqq_intraday_001",
            "symbol": "TQQQ",
            "side": "SELL",
            "order_type": "MKT",
            "state": "FILLED",
            "order_ref": "qqqv1:qqq_intraday_001:TQQQ:FLATTEN",
            "note": '{"decision_id":"d1","extras":{"reason":"flatten_eod"}}',
        }
    )
    kernel.pg = pg

    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)

    handler = FillEventHandler(adapter, kernel, notification_manager=manager)
    handler._is_fill_persisted = AsyncMock(return_value=True)
    await handler._process_fill(_mk_trade(order_id=4, perm_id=11), _mk_fill(exec_id="x1", perm_id=11))

    manager.publish.assert_called_once()
    event = manager.publish.call_args.args[0]
    assert event.event_type == "order_fill"
    assert event.status == "FILLED"
    assert event.order_role == "FLATTEN"
    assert event.amount_usd == 100.0


@pytest.mark.anyio
async def test_fill_event_uses_order_ref_strategy_fallback_when_db_context_missing():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()

    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value=None)
    kernel.pg = pg

    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)

    handler = FillEventHandler(adapter, kernel, notification_manager=manager)
    handler._is_fill_persisted = AsyncMock(return_value=True)
    await handler._process_fill(_mk_trade(order_id=41, perm_id=4101), _mk_fill(exec_id="x41", perm_id=4101))

    manager.publish.assert_called_once()
    event = manager.publish.call_args.args[0]
    assert event.event_type == "order_fill"
    assert event.strategy_id == "qqq_intraday_001"


@pytest.mark.anyio
async def test_fill_event_skips_notification_when_fill_not_persisted():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(return_value=None)

    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)

    handler = FillEventHandler(adapter, kernel, notification_manager=manager)
    handler._is_fill_persisted = AsyncMock(return_value=False)
    await handler._process_fill(_mk_trade(order_id=42, perm_id=4201), _mk_fill(exec_id="x42", perm_id=4201))

    manager.publish.assert_not_called()


@pytest.mark.anyio
async def test_is_fill_persisted_retries_and_succeeds():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(side_effect=[None, {"ok": 1}])
    handler = FillEventHandler(adapter, kernel)
    handler._fill_persist_check_attempts = 2
    handler._fill_persist_check_sleep_sec = 0.0

    ok = await handler._is_fill_persisted(
        fill_obj=Fill(
            fill_id="f1",
            order_id=1,
            order_ref="x",
            symbol="SQQQ",
            qty=1.0,
            price=10.0,
            ts_us=1,
            commission=0.0,
            exec_id="exec-1",
            perm_id=11,
        )
    )

    assert ok is True
    assert kernel.pg.fetchone.await_count == 2
    first_call = kernel.pg.fetchone.await_args_list[0]
    assert first_call.args[1] == "exec-1"
    assert first_call.args[2] == "1"
    assert first_call.args[3] == 11


@pytest.mark.anyio
async def test_is_fill_persisted_db_error_publishes_reconciliation_alert_once():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(side_effect=RuntimeError("db_down"))
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)

    handler = FillEventHandler(adapter, kernel, notification_manager=manager)
    handler._fill_persist_check_attempts = 2
    handler._fill_persist_check_sleep_sec = 0.0
    handler._fill_persist_alert_cooldown_sec = 9999.0

    fill_obj = Fill(
        fill_id="f2",
        order_id=2,
        order_ref="x",
        symbol="SQQQ",
        qty=1.0,
        price=10.0,
        ts_us=1,
        commission=0.0,
        exec_id="exec-2",
        perm_id=22,
    )
    ok = await handler._is_fill_persisted(fill_obj=fill_obj)
    assert ok is False
    # call again during cooldown -> no second alert
    ok2 = await handler._is_fill_persisted(fill_obj=fill_obj)
    assert ok2 is False

    calls = [c.args[0] for c in manager.publish.call_args_list]
    alerts = [e for e in calls if getattr(e, "event_type", "") == "reconciliation_alert"]
    assert len(alerts) == 1


@pytest.mark.anyio
async def test_cancelled_order_notification_disabled_by_default():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_cancel = AsyncMock()
    kernel.on_reject = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(return_value=None)
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)

    handler = FillEventHandler(adapter, kernel, notification_manager=manager)
    handler._running = True

    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=501, orderRef="tp:s1:TQQQ:abc", permId=1501, action="SELL", orderType="STP", totalQuantity=5),
        contract=SimpleNamespace(symbol="TQQQ"),
        orderStatus=SimpleNamespace(status="Cancelled", whyHeld="", filled=0.0, remaining=5.0, avgFillPrice=0.0),
    )

    await handler._process_order_status(trade)

    kernel.on_cancel.assert_awaited_once_with(501, reason="ib_order_status:cancelled")
    manager.publish.assert_not_called()


@pytest.mark.anyio
async def test_cancelled_order_notification_publishes_when_flag_enabled():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_cancel = AsyncMock()
    kernel.on_reject = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(
        return_value={
            "order_id": "77",
            "strategy_id": "qqq_intraday_001",
            "symbol": "TQQQ",
            "side": "SELL",
            "order_type": "STP",
            "state": "CANCELED",
            "order_ref": "qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:abc12345",
            "note": '{"decision_id":"d1","extras":{"reason":"protective_stop"}}',
        }
    )
    manager = MagicMock()
    manager.publish = MagicMock(return_value=True)

    handler = FillEventHandler(adapter, kernel, notification_manager=manager)
    handler._running = True
    handler._cancel_notification_enabled = True

    trade = SimpleNamespace(
        order=SimpleNamespace(
            orderId=777,
            orderRef="qqqv1:qqq_intraday_001:TQQQ:STOP:TRG:abc12345",
            permId=1777,
            action="SELL",
            orderType="STP",
            totalQuantity=5,
        ),
        contract=SimpleNamespace(symbol="TQQQ"),
        orderStatus=SimpleNamespace(status="Cancelled", whyHeld="", filled=0.0, remaining=5.0, avgFillPrice=0.0),
    )

    await handler._process_order_status(trade)

    kernel.on_cancel.assert_awaited_once_with(777, reason="ib_order_status:cancelled")
    manager.publish.assert_called_once()
    event = manager.publish.call_args.args[0]
    assert event.event_type == "order_cancelled"
    assert event.broker_order_id == "777"
    assert event.strategy_id == "qqq_intraday_001"
    assert event.order_role == "STOP_LOSS"
