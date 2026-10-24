import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Avoid importing heavy DB/config dependencies during module import.
stub_safety_kernel = types.ModuleType("services.exec.safety_kernel")
stub_safety_kernel.SafetyKernel = object
_original_safety_kernel = sys.modules.get("services.exec.safety_kernel")
sys.modules["services.exec.safety_kernel"] = stub_safety_kernel

from services.exec.fill_event_handler import FillEventHandler
from services.exec.models import Fill

# Restore original module reference so this stub does not leak to other tests.
if _original_safety_kernel is not None:
    sys.modules["services.exec.safety_kernel"] = _original_safety_kernel
else:
    del sys.modules["services.exec.safety_kernel"]


class _AuditWriter:
    def __init__(self):
        self.records = []

    def emit(self, **kwargs):
        self.records.append(kwargs)


def test_fill_handler_emits_fill_event_when_trace_present(monkeypatch):
    adapter = MagicMock()
    adapter.ib = MagicMock()
    adapter.ib.execDetailsEvent = MagicMock()

    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={
        "order_id": "11111111-1111-1111-1111-111111111111",
        "strategy_id": "s1",
        "symbol": "TQQQ",
        "side": "BUY",
        "order_type": "MKT",
        "state": "SUBMITTED",
        "order_ref": "r1",
        "note": '{"decision_id":"d1","trace":{"trace_id":"t1","strategy_run_id":"r1","trigger_id":"g1"}}',
    })
    kernel = MagicMock()
    kernel.pg = pg
    kernel.on_fill = AsyncMock(return_value=None)

    h = FillEventHandler(adapter, kernel, order_event_enabled=True)
    h._publish_fill_notification = AsyncMock(return_value=None)
    emit_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("services.exec.fill_event_handler.emit_order_event", emit_mock)

    fill = MagicMock()
    fill.execution.execId = "e1"
    fill.execution.shares = 1
    fill.execution.price = 10
    fill.execution.permId = 123
    fill.execution.exchange = "SMART"
    fill.commissionReport = None
    fill.time = MagicMock()
    fill.time.timestamp.return_value = 1700000000.0

    trade = MagicMock()
    trade.order.orderId = 456
    trade.order.orderRef = "r1"
    trade.contract.symbol = "TQQQ"

    asyncio.run(h._process_fill(trade, fill))

    # one fetch by perm_id path
    assert pg.fetchone.await_count >= 1
    emit_mock.assert_awaited_once()
    assert emit_mock.await_args.kwargs["event_type"] == "fill_received"


@pytest.mark.asyncio
async def test_fill_notification_suppresses_audit_self_heal_stop_fill():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    notification_manager = MagicMock()
    notification_manager.publish = MagicMock(return_value=True)

    h = FillEventHandler(adapter, kernel, notification_manager=notification_manager, order_event_enabled=False)
    h._resolve_order_context = AsyncMock(
        return_value={
            "strategy_id": "qqq_intraday_001",
            "symbol": "SQQQ",
            "side": "SELL",
            "order_type": "STP",
            "state": "FILLED",
            "order_ref": "qqqv1:qqq_intraday_001:SQQQ:STOP:abc",
            "reason": "audit_self_heal",
        }
    )

    trade = MagicMock()
    trade.order.orderId = 21
    trade.order.orderRef = "qqqv1:qqq_intraday_001:SQQQ:STOP:abc"
    trade.order.orderType = "STP"
    trade.order.action = "SELL"
    trade.contract.symbol = "SQQQ"
    trade.orderStatus = SimpleNamespace(status="Filled")

    fill = MagicMock()
    fill.execution.side = "SLD"
    fill.execution.shares = 81
    fill.execution.price = 85.22
    fill.execution.exchange = "SMART"

    fill_obj = Fill(
        fill_id="f-audit-stop",
        order_id=21,
        order_ref="qqqv1:qqq_intraday_001:SQQQ:STOP:abc",
        symbol="SQQQ",
        perm_id=2101,
        ts_us=1_700_000_000_000_000,
        qty=81.0,
        price=85.22,
        commission=0.0,
        exec_id="e-audit-stop",
    )

    await h._publish_fill_notification(trade=trade, fill=fill, fill_obj=fill_obj)

    notification_manager.publish.assert_not_called()


@pytest.mark.asyncio
async def test_emit_fill_event_audits_missing_reason_context():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    writer = _AuditWriter()

    h = FillEventHandler(adapter, kernel, audit_writer=writer, order_event_enabled=False)
    h._resolve_order_context = AsyncMock(
        return_value={
            "order_id": "11111111-1111-1111-1111-111111111111",
            "strategy_id": "qqq_intraday_001",
            "symbol": "SQQQ",
            "side": "BUY",
            "order_type": "MKT",
            "state": "FILLED",
            "order_ref": "qqqv1:qqq_intraday_001:SQQQ:FLATTEN:test",
            "decision_id": "",
            "reason": "",
            "trace": {},
            "attribution": {"path": "broker_order_id", "fallback_used": True, "ambiguous": False, "candidate_count": 2},
        }
    )

    fill_obj = Fill(
        fill_id="f-missing-reason",
        order_id=32,
        order_ref="qqqv1:qqq_intraday_001:SQQQ:FLATTEN:test",
        symbol="SQQQ",
        perm_id=3201,
        ts_us=1_700_000_000_000_000,
        qty=81.0,
        price=85.22,
        commission=0.0,
        exec_id="e-missing-reason",
    )

    await h._emit_fill_event(fill_obj=fill_obj)

    record = next(row for row in writer.records if row["stage"] == "fill_context_missing_fields")
    assert record["payload"]["missing_reason"] is True
    assert record["payload"]["missing_decision_id"] is True
    assert record["payload"]["candidate_count"] == 2


@pytest.mark.asyncio
async def test_emit_fill_event_skips_missing_context_audit_when_reason_inferred(monkeypatch):
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.pg = MagicMock()
    writer = _AuditWriter()

    h = FillEventHandler(adapter, kernel, audit_writer=writer, order_event_enabled=False)
    h._resolve_order_context = AsyncMock(
        return_value={
            "order_id": "11111111-1111-1111-1111-111111111111",
            "strategy_id": "spy_1dte_ma10_reject_0324_001",
            "symbol": "OPT:SPY:20260402:655:C",
            "side": "SELL",
            "order_type": "MKT",
            "state": "FILLED",
            "order_ref": "optk:optk.spy_ma_cross_stack_cont_v2_dual.spy",
            "decision_id": "d1",
            "reason": "exit",
            "trace": {"trace_id": "t1", "strategy_run_id": "r1", "trigger_id": "g1"},
            "attribution": {"path": "order_ref", "fallback_used": False, "ambiguous": False, "candidate_count": 1},
        }
    )
    monkeypatch.setattr("services.exec.fill_event_handler.emit_order_event", AsyncMock(return_value=True))

    fill_obj = Fill(
        fill_id="f-exit-clean",
        order_id=32,
        order_ref="optk:optk.spy_ma_cross_stack_cont_v2_dual.spy",
        symbol="OPT:SPY:20260402:655:C",
        perm_id=3201,
        ts_us=1_700_000_000_000_000,
        qty=4.0,
        price=3.85,
        commission=0.0,
        exec_id="e-exit-clean",
    )

    await h._emit_fill_event(fill_obj=fill_obj)

    stages = [row["stage"] for row in writer.records]
    assert "fill_context_missing_fields" not in stages
    assert "fill_attribution_resolved" in stages
    assert "fill_received" in stages


@pytest.mark.asyncio
async def test_emit_close_only_completion_audit_confirms_flatten_completion():
    adapter = MagicMock()
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    pg.fetchval = AsyncMock(return_value=0)
    kernel = MagicMock()
    kernel.pg = pg
    writer = _AuditWriter()

    h = FillEventHandler(adapter, kernel, audit_writer=writer, order_event_enabled=False)

    fill_obj = Fill(
        fill_id="f-flat-confirmed",
        order_id=32,
        order_ref="optk:spy:flatten",
        symbol="OPT:SPY:20260402:655:C",
        perm_id=3201,
        ts_us=1_700_000_000_000_000,
        qty=4.0,
        price=3.85,
        commission=0.0,
        exec_id="e-flat-confirmed",
    )
    await h._emit_close_only_completion_audit(
        ctx={
            "strategy_id": "spy_1dte_ma10_reject_0324_001",
            "symbol": "OPT:SPY:20260402:655:C",
            "decision_id": "d1",
            "trace": {"trace_id": "t1", "strategy_run_id": "r1", "trigger_id": "g1"},
            "extras": {"flatten_action": True, "close_only": True},
        },
        fill_obj=fill_obj,
    )

    record = next(row for row in writer.records if row["stage"] == "flatten_completion_confirmed")
    assert record["status"] == "confirmed"
    assert record["payload"]["pos_qty"] == 0.0
    assert record["payload"]["open_order_count"] == 0


@pytest.mark.asyncio
async def test_emit_close_only_completion_audit_marks_pending_when_position_remains():
    adapter = MagicMock()
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": -4.0})
    pg.fetchval = AsyncMock(return_value=1)
    kernel = MagicMock()
    kernel.pg = pg
    writer = _AuditWriter()

    h = FillEventHandler(adapter, kernel, audit_writer=writer, order_event_enabled=False)

    fill_obj = Fill(
        fill_id="f-flat-pending",
        order_id=33,
        order_ref="optk:spy:flatten",
        symbol="OPT:SPY:20260402:655:C",
        perm_id=3301,
        ts_us=1_700_000_000_000_000,
        qty=2.0,
        price=3.80,
        commission=0.0,
        exec_id="e-flat-pending",
    )
    await h._emit_close_only_completion_audit(
        ctx={
            "strategy_id": "spy_1dte_ma10_reject_0324_001",
            "symbol": "OPT:SPY:20260402:655:C",
            "decision_id": "d2",
            "trace": {"trace_id": "t2", "strategy_run_id": "r2", "trigger_id": "g2"},
            "extras": {"flatten_action": True, "close_only": True},
        },
        fill_obj=fill_obj,
    )

    record = next(row for row in writer.records if row["stage"] == "flatten_completion_pending")
    assert record["status"] == "pending"
    assert record["payload"]["pos_qty"] == -4.0
    assert record["payload"]["open_order_count"] == 1
