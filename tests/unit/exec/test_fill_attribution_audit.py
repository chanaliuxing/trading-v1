from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

from services.exec.fill_event_handler import FillEventHandler
from services.exec.models import Fill


@pytest.mark.asyncio
async def test_resolve_order_context_disables_broker_order_id_fallback_by_default():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(return_value=None)
    handler = FillEventHandler(adapter, kernel)

    ctx = await handler._resolve_order_context(
        fill_obj=Fill(
            fill_id="f1",
            order_id=101,
            order_ref="",
            symbol="TQQQ",
            qty=1.0,
            price=10.0,
            ts_us=1,
            commission=0.0,
            exchange="SMART",
            exec_id="e1",
            perm_id=None,
        )
    )

    attribution = dict(ctx.get("attribution") or {})
    assert attribution["path"] == "broker_order_id"
    assert attribution["matched"] is False
    assert attribution["fallback_used"] is False
    assert attribution["fallback_disabled"] is True
    kernel.pg.fetchone.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_order_context_ignores_stale_broker_order_id_fallback_candidate():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    stale_ts = datetime.now(timezone.utc) - timedelta(hours=12)
    kernel.pg.fetchone = AsyncMock(return_value={
        "order_id": "111",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "side": "BUY",
        "order_type": "MKT",
        "state": "SUBMITTED",
        "order_ref": "r1",
        "note": '{"decision_id":"d1","trace":{"trace_id":"t1"}}',
        "candidate_count": 1,
        "created_at": stale_ts,
        "updated_at": stale_ts,
    })
    handler = FillEventHandler(adapter, kernel)
    handler._broker_order_id_fallback_recent_sec = 300.0
    handler._allow_broker_order_id_context_fallback = True

    ctx = await handler._resolve_order_context(
        fill_obj=Fill(
            fill_id="f1",
            order_id=101,
            order_ref="",
            symbol="TQQQ",
            qty=1.0,
            price=10.0,
            ts_us=1,
            commission=0.0,
            exchange="SMART",
            exec_id="e1",
            perm_id=None,
        )
    )

    attribution = dict(ctx.get("attribution") or {})
    assert attribution["path"] == "broker_order_id"
    assert attribution["matched"] is False
    assert attribution["fallback_used"] is True
    assert attribution["stale_candidate_ignored"] is True


@pytest.mark.asyncio
async def test_resolve_order_context_from_status_ignores_stale_broker_order_id_fallback_candidate():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    stale_ts = datetime.now(timezone.utc) - timedelta(hours=12)
    kernel.pg.fetchone = AsyncMock(return_value={
        "order_id": "111",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "side": "BUY",
        "order_type": "MKT",
        "state": "SUBMITTED",
        "order_ref": "r1",
        "note": '{"decision_id":"d1","trace":{"trace_id":"t1"}}',
        "created_at": stale_ts,
        "updated_at": stale_ts,
    })
    handler = FillEventHandler(adapter, kernel)
    handler._broker_order_id_fallback_recent_sec = 300.0
    handler._allow_broker_order_id_context_fallback = True

    ctx = await handler._resolve_order_context_from_status(
        order_id=101,
        perm_id=None,
        order_ref="",
    )

    assert ctx == {}


@pytest.mark.asyncio
async def test_resolve_order_context_from_status_disables_broker_order_id_fallback_by_default():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(return_value=None)
    handler = FillEventHandler(adapter, kernel)

    ctx = await handler._resolve_order_context_from_status(
        order_id=101,
        perm_id=None,
        order_ref="",
    )

    assert ctx == {}
    kernel.pg.fetchone.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_fill_event_emits_fallback_attribution_audit(monkeypatch):
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    handler = FillEventHandler(adapter, kernel, order_event_enabled=True, audit_writer=MagicMock())
    handler._resolve_order_context = AsyncMock(return_value={
        "order_id": "111",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "trace": {"trace_id": "t1", "strategy_run_id": "r1", "trigger_id": "g1"},
        "decision_id": "d1",
        "attribution": {"path": "broker_order_id", "fallback_used": True, "ambiguous": False, "candidate_count": 1},
    })
    monkeypatch.setattr("services.exec.fill_event_handler.emit_order_event", AsyncMock(return_value=True))

    await handler._emit_fill_event(fill_obj=Fill(
        fill_id="f1",
        order_id=101,
        order_ref="r1",
        symbol="TQQQ",
        qty=1.0,
        price=10.0,
        ts_us=1,
        commission=0.0,
        exchange="SMART",
        exec_id="e1",
        perm_id=123,
    ))

    stages = [call.kwargs.get("stage") for call in handler.audit_writer.emit.call_args_list]
    assert "fill_attribution_fallback_broker_order_id" in stages
    assert "fill_received" in stages


@pytest.mark.asyncio
async def test_resolve_order_context_infers_exit_reason_from_extras_when_reason_missing():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(
        return_value={
            "order_id": "111",
            "strategy_id": "spy_1dte_ma10_reject_0324_001",
            "symbol": "OPT:SPY:20260402:655:C",
            "side": "SELL",
            "order_type": "MKT",
            "state": "SUBMITTED",
            "order_ref": "optk:optk.spy_ma_cross_stack_cont_v2_dual.spy",
            "note": '{"decision_id":"d1","extras":{"close_only":true,"intent_action":"exit"},"trace":{"trace_id":"t1"}}',
        }
    )
    handler = FillEventHandler(adapter, kernel)

    ctx = await handler._resolve_order_context(
        fill_obj=Fill(
            fill_id="f1",
            order_id=101,
            order_ref="optk:optk.spy_ma_cross_stack_cont_v2_dual.spy",
            symbol="OPT:SPY:20260402:655:C",
            qty=1.0,
            price=10.0,
            ts_us=1,
            commission=0.0,
            exchange="SMART",
            exec_id="e1",
            perm_id=None,
        )
    )

    assert ctx["decision_id"] == "d1"
    assert ctx["reason"] == "exit"


@pytest.mark.asyncio
async def test_resolve_order_context_retries_until_db_row_visible():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(
        side_effect=[
            None,  # perm_id first pass
            None,  # order_ref first pass
            None,  # broker_order_id first pass
            {
                "order_id": "111",
                "strategy_id": "qqq_intraday_001",
                "symbol": "SQQQ",
                "side": "BUY",
                "order_type": "MKT",
                "state": "SUBMITTED",
                "order_ref": "qqqv1:qqq_intraday_001:SQQQ:ENTRY:TRG:abc",
                "note": '{"decision_id":"d1","extras":{"reason":"entry"},"trace":{"trace_id":"t1"}}',
            },
        ]
    )
    handler = FillEventHandler(adapter, kernel)
    handler._context_retry_attempts = 1
    handler._context_retry_sleep_sec = 0.000001

    ctx = await handler._resolve_order_context(
        fill_obj=Fill(
            fill_id="f1",
            order_id=410,
            order_ref="qqqv1:qqq_intraday_001:SQQQ:ENTRY:TRG:abc",
            symbol="SQQQ",
            qty=102.0,
            price=67.73,
            ts_us=1,
            commission=0.0,
            exchange="SMART",
            exec_id="e1",
            perm_id=974608945,
        )
    )

    assert ctx["strategy_id"] == "qqq_intraday_001"
    assert ctx["decision_id"] == "d1"
    attribution = dict(ctx.get("attribution") or {})
    assert attribution["matched"] is True
    assert attribution["retry_used"] is True
    assert attribution["retry_count"] == 1
