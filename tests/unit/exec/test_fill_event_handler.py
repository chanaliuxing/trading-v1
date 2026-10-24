import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.exec.fill_event_handler as fill_mod
from services.exec.fill_event_handler import FillEventHandler
from services.exec.safety_kernel import SafetyKernel
from services.reconciliation.broker_truth_convergence import ConvergenceResult


class _AuditWriter:
    def __init__(self):
        self.records = []

    def emit(self, **kwargs):
        self.records.append(kwargs)


def _mk_trade(order_id: int = 4, perm_id: int | None = None):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, orderRef="qqqv1:qqq_intraday_001:TQQQ:ENTRY", permId=perm_id),
        contract=SimpleNamespace(symbol="TQQQ"),
        orderStatus=SimpleNamespace(
            status="Submitted",
            filled=0.0,
            remaining=1.0,
            avgFillPrice=0.0,
            whyHeld="",
        ),
    )


def _mk_fill(exec_id: str = "e1", perm_id: int | None = None):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id,
            shares=1.0,
            price=48.18,
            exchange="SMART",
            permId=perm_id,
        ),
        commissionReport=SimpleNamespace(commission=0.0),
        time=SimpleNamespace(timestamp=lambda: 1_700_000_000.0),
    )


@pytest.mark.asyncio
async def test_process_fill_uses_execution_perm_id_when_available():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    handler = FillEventHandler(adapter, kernel)
    handler._is_fill_persisted = AsyncMock(return_value=True)

    await handler._process_fill(_mk_trade(order_id=4, perm_id=111), _mk_fill(exec_id="x1", perm_id=222))

    kernel.on_fill.assert_awaited_once()
    fill_obj = kernel.on_fill.await_args.args[0]
    assert fill_obj.perm_id == 222


@pytest.mark.asyncio
async def test_process_fill_falls_back_to_order_perm_id():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    handler = FillEventHandler(adapter, kernel)
    handler._is_fill_persisted = AsyncMock(return_value=True)

    await handler._process_fill(_mk_trade(order_id=5, perm_id=333), _mk_fill(exec_id="x2", perm_id=None))

    kernel.on_fill.assert_awaited_once()
    fill_obj = kernel.on_fill.await_args.args[0]
    assert fill_obj.perm_id == 333


@pytest.mark.asyncio
async def test_process_fill_survives_fill_event_emit_failure(monkeypatch):
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(return_value=None)
    handler = FillEventHandler(adapter, kernel, order_event_enabled=True)
    handler._is_fill_persisted = AsyncMock(return_value=True)

    monkeypatch.setattr(fill_mod, "emit_order_event", AsyncMock(side_effect=RuntimeError("event_down")))

    await handler._process_fill(_mk_trade(order_id=6, perm_id=999), _mk_fill(exec_id="x3", perm_id=999))

    kernel.on_fill.assert_awaited_once()
    assert "x3:6" in handler.processed_fills


@pytest.mark.asyncio
async def test_process_fill_retries_when_fill_not_persisted_initially():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.execute = AsyncMock(return_value="OK")
    writer = _AuditWriter()

    handler = FillEventHandler(adapter, kernel, audit_writer=writer)
    handler._post_fill_reconcile_attempts = 2
    handler._post_fill_reconcile_sleep_sec = 0.001
    handler._fill_persist_check_attempts = 1

    persisted = iter([False, False, True])

    async def _is_persisted(*, fill_obj):
        return next(persisted)

    kernel.backfill_fill_after_order_visible = AsyncMock(
        return_value={"resolved": True, "persisted": True, "fill_inserted": True, "state_updated": True}
    )
    handler._is_fill_persisted = AsyncMock(side_effect=_is_persisted)
    handler._emit_fill_event = AsyncMock(return_value=None)
    handler._publish_fill_notification = AsyncMock(return_value=None)

    await handler._process_fill(_mk_trade(order_id=61, perm_id=611), _mk_fill(exec_id="x61", perm_id=611))

    assert kernel.on_fill.await_count == 1
    kernel.backfill_fill_after_order_visible.assert_awaited_once()
    assert "x61:61" in handler.processed_fills
    assert any(r["stage"] == "fill_persist_recovered_after_retry" for r in writer.records)


@pytest.mark.asyncio
async def test_process_fill_does_not_consume_fill_when_persist_never_observed():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.execute = AsyncMock(return_value="OK")
    writer = _AuditWriter()

    handler = FillEventHandler(adapter, kernel, audit_writer=writer)
    handler._post_fill_reconcile_attempts = 2
    handler._post_fill_reconcile_sleep_sec = 0.001
    handler._fill_persist_check_attempts = 1
    kernel.backfill_fill_after_order_visible = AsyncMock(return_value={"resolved": False, "persisted": False})
    handler._is_fill_persisted = AsyncMock(return_value=False)
    handler._emit_fill_event = AsyncMock(return_value=None)
    handler._publish_fill_notification = AsyncMock(return_value=None)

    await handler._process_fill(_mk_trade(order_id=62, perm_id=622), _mk_fill(exec_id="x62", perm_id=622))

    assert kernel.on_fill.await_count == 1
    assert kernel.backfill_fill_after_order_visible.await_count == 2
    assert "x62:62" not in handler.processed_fills
    handler._emit_fill_event.assert_not_called()
    handler._publish_fill_notification.assert_not_called()
    assert any(r["stage"] == "fill_persist_missing_after_retry" for r in writer.records)
    assert any(r["stage"] == "unresolved_broker_event_quarantined" for r in writer.records)
    assert kernel.pg.execute.await_count >= 1


@pytest.mark.asyncio
async def test_process_fill_converges_unattributed_fill_before_quarantine():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.execute = AsyncMock(return_value="OK")
    writer = _AuditWriter()

    handler = FillEventHandler(adapter, kernel, audit_writer=writer)
    handler._post_fill_reconcile_attempts = 1
    handler._post_fill_reconcile_sleep_sec = 0.001
    handler._fill_persist_check_attempts = 1
    kernel.backfill_fill_after_order_visible = AsyncMock(return_value={"resolved": False, "persisted": False})
    handler._is_fill_persisted = AsyncMock(return_value=False)
    handler.convergence_service.converge_unattributed_fill = AsyncMock(
        return_value=ConvergenceResult(
            ok=True,
            reason="unattributed_fill_converged",
            metadata={"order_id": "adopted-order-1"},
        )
    )
    handler._emit_fill_event = AsyncMock(return_value=None)
    handler._resolve_order_context = AsyncMock(return_value={"side": "SELL", "order_ref": "ref-1", "strategy_id": "adopted_reconciler", "symbol": "TQQQ"})
    handler._emit_close_only_completion_audit = AsyncMock(return_value=None)
    handler._publish_fill_notification = AsyncMock(return_value=None)
    trade = _mk_trade(order_id=66, perm_id=666)
    trade.order.action = "SELL"

    await handler._process_fill(trade, _mk_fill(exec_id="x66", perm_id=666))

    assert "x66:66" in handler.processed_fills
    handler.convergence_service.converge_unattributed_fill.assert_awaited_once()
    assert any(r["stage"] == "unattributed_fill_converged" for r in writer.records)
    assert not any(r["stage"] == "unresolved_broker_event_quarantined" for r in writer.records)


@pytest.mark.asyncio
async def test_process_fill_real_kernel_backfills_after_order_row_becomes_visible():
    adapter = MagicMock()
    writer = _AuditWriter()

    broker = MagicMock()
    reservation = MagicMock()
    reservation.release_on_terminal = AsyncMock(return_value=(True, 100.0))
    flatten_controller = MagicMock()
    flatten_controller.resolve_from_position = AsyncMock(return_value=SimpleNamespace(state="confirmed"))
    pg = MagicMock()

    state = {"fill_visible": False, "resolve_calls": 0}

    async def _fetchone(sql, *args):
        if "FROM core.core_fill" in sql:
            return {"ok": 1} if state["fill_visible"] else None
        return None

    pg.fetchone = AsyncMock(side_effect=_fetchone)
    pg.execute = AsyncMock(return_value="OK")

    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=reservation,
        flatten_controller=flatten_controller,
    )
    resolved_row = {
        "order_id": "uuid-real-kernel-backfill",
        "idempotency_key": "idem-real-kernel-backfill",
        "qty": 5.0,
        "strategy_id": "qqq_intraday_001",
        "symbol": "SQQQ",
        "side": "SELL",
        "state": "SUBMITTED",
        "order_ref": "qqqv1:qqq_intraday_001:SQQQ:FLATTEN:TRG:test",
        "note": '{"decision_id":"dec-real-kernel-backfill","extras":{"flatten_action":true,"intent_action":"flatten"}}',
    }
    async def _resolve(fill_obj):
        state["resolve_calls"] += 1
        if state["resolve_calls"] == 1:
            return None
        return resolved_row

    async def _persist(fill_obj, order_uuid):
        state["fill_visible"] = True
        return True

    kernel._resolve_order_row_for_fill = AsyncMock(side_effect=_resolve)
    kernel._persist_fill = AsyncMock(side_effect=_persist)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)
    kernel._get_filled_qty = AsyncMock(return_value=5.0)
    kernel._update_order_state = AsyncMock(return_value=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)

    handler = FillEventHandler(adapter, kernel, audit_writer=writer)
    handler._post_fill_reconcile_attempts = 2
    handler._post_fill_reconcile_sleep_sec = 0.001
    handler._fill_persist_check_attempts = 1
    handler._emit_fill_event = AsyncMock(return_value=None)
    handler._publish_fill_notification = AsyncMock(return_value=None)

    await handler._process_fill(_mk_trade(order_id=64, perm_id=644), _mk_fill(exec_id="x64", perm_id=644))

    assert "x64:64" in handler.processed_fills
    assert kernel._resolve_order_row_for_fill.await_count >= 2
    kernel._persist_fill.assert_awaited_once()
    kernel._apply_fill_to_core_position.assert_awaited_once()
    kernel._update_order_state.assert_awaited_once()
    kernel._finalize_decision_terminal.assert_awaited_once_with(
        decision_id="dec-real-kernel-backfill",
        terminal_state="FILLED",
    )
    assert reservation.release_on_terminal.await_count == 1
    flatten_controller.resolve_from_position.assert_awaited_once()
    assert any(
        r["stage"] == "fill_persist_recovered_after_retry"
        and r["reason"] == "fill_backfilled_after_order_visible"
        for r in writer.records
    )


@pytest.mark.asyncio
async def test_process_fill_real_kernel_unresolved_stays_unprocessed_and_emits_missing_stage():
    adapter = MagicMock()
    writer = _AuditWriter()

    broker = MagicMock()
    reservation = MagicMock()
    reservation.release_on_terminal = AsyncMock(return_value=(True, 100.0))
    flatten_controller = MagicMock()
    flatten_controller.resolve_from_position = AsyncMock(return_value=SimpleNamespace(state="confirmed"))
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value="OK")

    kernel = SafetyKernel(
        broker_adapter=broker,
        pg=pg,
        reservation_service=reservation,
        flatten_controller=flatten_controller,
    )
    kernel._resolve_order_row_for_fill = AsyncMock(return_value=None)
    kernel._persist_fill = AsyncMock(return_value=False)
    kernel._apply_fill_to_core_position = AsyncMock(return_value=None)
    kernel._get_filled_qty = AsyncMock(return_value=0.0)
    kernel._update_order_state = AsyncMock(return_value=None)
    kernel._finalize_decision_terminal = AsyncMock(return_value=None)

    handler = FillEventHandler(adapter, kernel, audit_writer=writer)
    handler._post_fill_reconcile_attempts = 2
    handler._post_fill_reconcile_sleep_sec = 0.001
    handler._fill_persist_check_attempts = 1
    handler._emit_fill_event = AsyncMock(return_value=None)
    handler._publish_fill_notification = AsyncMock(return_value=None)

    await handler._process_fill(_mk_trade(order_id=65, perm_id=655), _mk_fill(exec_id="x65", perm_id=655))

    assert "x65:65" not in handler.processed_fills
    assert kernel._resolve_order_row_for_fill.await_count >= 3
    kernel._persist_fill.assert_not_awaited()
    kernel._apply_fill_to_core_position.assert_not_awaited()
    kernel._update_order_state.assert_not_awaited()
    kernel._finalize_decision_terminal.assert_not_awaited()
    reservation.release_on_terminal.assert_not_awaited()
    flatten_controller.resolve_from_position.assert_not_awaited()
    handler._emit_fill_event.assert_not_called()
    handler._publish_fill_notification.assert_not_called()
    assert any(r["stage"] == "fill_persist_missing_after_retry" for r in writer.records)
    assert any(r["stage"] == "unresolved_broker_event_quarantined" for r in writer.records)
    assert pg.execute.await_count >= 1


@pytest.mark.asyncio
async def test_process_fill_kernel_failure_allows_retry_of_same_fill():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock(side_effect=[RuntimeError("kernel_fail"), None])
    handler = FillEventHandler(adapter, kernel)
    handler._is_fill_persisted = AsyncMock(return_value=True)

    trade = _mk_trade(order_id=7, perm_id=77)
    fill = _mk_fill(exec_id="x7", perm_id=77)

    await handler._process_fill(trade, fill)
    await handler._process_fill(trade, fill)

    assert kernel.on_fill.await_count == 2


@pytest.mark.asyncio
async def test_process_fill_concurrent_retry_after_first_kernel_failure():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_fill = AsyncMock(side_effect=[RuntimeError("kernel_fail"), None])
    handler = FillEventHandler(adapter, kernel)
    handler._is_fill_persisted = AsyncMock(return_value=True)

    trade = _mk_trade(order_id=8, perm_id=88)
    fill = _mk_fill(exec_id="x8", perm_id=88)

    await asyncio.gather(
        handler._process_fill(trade, fill),
        handler._process_fill(trade, fill),
    )

    assert kernel.on_fill.await_count == 2
    assert "x8:8" in handler.processed_fills


@pytest.mark.asyncio
async def test_process_fill_takeover_owner_token_prevents_third_owner():
    adapter = MagicMock()
    kernel = MagicMock()
    calls = {"n": 0}

    async def _on_fill(_fill_obj):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.04)
            raise RuntimeError("first_owner_fail")
        if calls["n"] == 2:
            await asyncio.sleep(0.015)
            return None
        raise AssertionError("unexpected third owner")

    kernel.on_fill = AsyncMock(side_effect=_on_fill)
    handler = FillEventHandler(adapter, kernel)
    handler._fill_inflight_wait_timeout_sec = 0.01
    handler._fill_inflight_takeover_sec = 0.03
    handler._is_fill_persisted = AsyncMock(return_value=True)

    trade = _mk_trade(order_id=9, perm_id=99)
    fill = _mk_fill(exec_id="x9", perm_id=99)

    async def _runner(delay: float):
        await asyncio.sleep(delay)
        await handler._process_fill(trade, fill)

    await asyncio.gather(
        _runner(0.0),
        _runner(0.001),
        _runner(0.046),
    )

    assert calls["n"] == 2
    assert "x9:9" in handler.processed_fills


@pytest.mark.asyncio
async def test_process_terminal_cancel_status_calls_kernel_on_cancel_once():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_cancel = AsyncMock()
    kernel.on_reject = AsyncMock()
    handler = FillEventHandler(adapter, kernel)
    handler._running = True

    trade = _mk_trade(order_id=17, perm_id=170)
    trade.order.orderRef = "qqqv1:qqq_intraday_001:SQQQ:STOP:TRG:abc"
    trade.orderStatus.status = "Cancelled"
    trade.orderStatus.whyHeld = "flatten sibling cancel"

    await handler._process_order_status(trade)
    await handler._process_order_status(trade)

    kernel.on_cancel.assert_awaited_once_with(17, reason="flatten sibling cancel")
    kernel.on_reject.assert_not_called()


@pytest.mark.asyncio
async def test_process_terminal_reject_status_calls_kernel_on_reject():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_cancel = AsyncMock()
    kernel.on_reject = AsyncMock()
    handler = FillEventHandler(adapter, kernel)
    handler._running = True

    trade = _mk_trade(order_id=23, perm_id=230)
    trade.orderStatus.status = "Rejected"
    trade.orderStatus.whyHeld = "risk_reject"

    await handler._process_order_status(trade)

    kernel.on_reject.assert_awaited_once_with(23, reason="risk_reject")
    kernel.on_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_process_inactive_status_defaults_reason_when_missing():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_cancel = AsyncMock()
    kernel.on_reject = AsyncMock()
    handler = FillEventHandler(adapter, kernel)
    handler._running = True

    trade = _mk_trade(order_id=29, perm_id=290)
    trade.orderStatus.status = "Inactive"
    trade.orderStatus.whyHeld = ""

    await handler._process_order_status(trade)

    kernel.on_cancel.assert_awaited_once_with(29, reason="ib_order_status:inactive")


@pytest.mark.asyncio
async def test_process_terminal_status_quarantines_unresolved_status_identity():
    adapter = MagicMock()
    kernel = MagicMock()
    kernel.on_cancel = AsyncMock()
    kernel.on_reject = AsyncMock()
    kernel.pg = MagicMock()
    kernel.pg.fetchone = AsyncMock(return_value=None)
    kernel.pg.execute = AsyncMock(return_value="OK")
    handler = FillEventHandler(adapter, kernel, audit_writer=_AuditWriter())
    handler._running = True

    trade = _mk_trade(order_id=31, perm_id=310)
    trade.orderStatus.status = "Cancelled"
    trade.orderStatus.whyHeld = "ib_cancelled"

    await handler._process_order_status(trade)

    kernel.on_cancel.assert_awaited_once_with(31, reason="ib_cancelled")
    assert kernel.pg.execute.await_count >= 1
    assert any(r["stage"] == "unresolved_broker_event_quarantined" for r in handler.audit_writer.records)
