from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.exec.flatten_controller import FlattenController
from services.strategy_runtime_v2 import InMemoryStateTransitionRecorder


@pytest.mark.asyncio
async def test_mark_submitted_by_idempotency_merges_flatten_lifecycle():
    pg = MagicMock()
    pg.fetchone = AsyncMock(
        return_value={
            "order_id": "db-order-1",
            "strategy_id": "qqq_intraday_001",
            "symbol": "TQQQ",
            "order_ref": "qqqv1:qqq_intraday_001:TQQQ:FLATTEN:TRG:abc",
            "note": '{"decision_id":"d1","flatten_lifecycle":{"state":"created"}}',
        }
    )
    sink = InMemoryStateTransitionRecorder()
    ctl = FlattenController(pg=pg, transition_sink=sink)

    payload = await ctl.mark_submitted_by_idempotency(
        idempotency_key="idem-1",
        order_id=101,
        close_only=True,
    )

    assert '"flatten_lifecycle"' in payload
    assert '"state": "submitted"' in payload or '"state":"submitted"' in payload
    events = sink.events()
    assert len(events) == 1
    assert events[0].state_machine == "flatten"
    assert events[0].strategy_id == "qqq_intraday_001"
    assert events[0].symbol == "TQQQ"
    assert events[0].order_ref == "qqqv1:qqq_intraday_001:TQQQ:FLATTEN:TRG:abc"
    assert events[0].before_state == "created"
    assert events[0].after_state == "submitted"


@pytest.mark.asyncio
async def test_resolve_from_position_marks_confirmed_and_unlocks_flatten_action():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    pg.fetchval = AsyncMock(return_value=0)
    pg.execute = AsyncMock(return_value="OK")
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value={"reason": "flatten_action"})
    lock_store.unlock = AsyncMock(return_value=True)
    sink = InMemoryStateTransitionRecorder()
    ctl = FlattenController(pg=pg, manual_intervention_lock_store=lock_store, transition_sink=sink)

    state = await ctl.resolve_from_position(
        order_uuid="11111111-1111-1111-1111-111111111111",
        resolved_order={
            "order_id": "11111111-1111-1111-1111-111111111111",
            "strategy_id": "spy_1dte_ma10_reject_0324_001",
            "symbol": "OPT:SPY:20260402:655:C",
            "note": '{"extras":{"flatten_action":true,"close_only":true}}',
        },
        confirmed_by="safety_kernel_fill",
        fill_broker_order_id=101,
        unlock_reason="close_only_fill_flattened",
    )

    assert state == "confirmed"
    lock_store.unlock.assert_awaited_once()
    merged = pg.execute.await_args.args[0]
    assert "UPDATE core.core_order" in merged
    assert sink.events()[-1].after_state == "confirmed"


@pytest.mark.asyncio
async def test_reconcile_confirmed_if_flat_marks_confirmed_without_unlock():
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"pos_qty": 0.0})
    pg.execute = AsyncMock(return_value="OK")
    lock_store = MagicMock()
    lock_store.get_active = AsyncMock(return_value={"reason": "flatten_action"})
    lock_store.unlock = AsyncMock(return_value=True)
    sink = InMemoryStateTransitionRecorder()
    ctl = FlattenController(pg=pg, manual_intervention_lock_store=lock_store, transition_sink=sink)

    changed = await ctl.reconcile_confirmed_if_flat(
        order_row={
            "order_id": "22222222-2222-2222-2222-222222222222",
            "strategy_id": "spy_1dte_ma10_reject_0324_001",
            "symbol": "OPT:SPY:20260402:655:C",
            "note": '{"flatten_lifecycle":{"state":"pending"}}',
        },
        ib_open_symbols=set(),
    )

    assert changed is True
    lock_store.unlock.assert_not_awaited()
    assert sink.events()[-1].before_state == "pending"
    assert sink.events()[-1].after_state == "confirmed"


@pytest.mark.asyncio
async def test_mark_rejected_by_idempotency_records_shadow_transition() -> None:
    pg = MagicMock()
    pg.fetchone = AsyncMock(return_value={"note": '{"decision_id":"d2","flatten_lifecycle":{"state":"submitted"}}'})
    sink = InMemoryStateTransitionRecorder()
    ctl = FlattenController(pg=pg, transition_sink=sink)

    payload = await ctl.mark_rejected_by_idempotency(
        idempotency_key="idem-2",
        reason="broker_rejected",
        close_only=False,
    )

    assert '"state": "rejected"' in payload or '"state":"rejected"' in payload
    assert sink.events()[-1].before_state == "submitted"
    assert sink.events()[-1].after_state == "rejected"


@pytest.mark.asyncio
async def test_probe_completion_prefers_order_truth_service() -> None:
    pg = MagicMock()
    truth = MagicMock()
    truth.effective_position = AsyncMock(
        return_value=SimpleNamespace(
            source="runtime_reconciled_flat",
            position=SimpleNamespace(pos_qty=0.0),
        )
    )
    truth.effective_open_orders = AsyncMock(
        return_value=SimpleNamespace(
            source="runtime_reconciled_truth",
            orders=[],
        )
    )
    ctl = FlattenController(pg=pg, order_truth_service=truth)

    result = await ctl.probe_completion(strategy_id="qqq_intraday_001", symbol="SQQQ")

    assert result["is_flat"] is True
    assert result["state"] == "confirmed"
    assert result["position_source"] == "runtime_reconciled_flat"
    assert result["open_orders_source"] == "runtime_reconciled_truth"
    pg.fetchone.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_confirmed_if_flat_uses_order_truth_service() -> None:
    pg = MagicMock()
    pg.execute = AsyncMock(return_value="OK")
    truth = MagicMock()
    truth.effective_position = AsyncMock(
        return_value=SimpleNamespace(
            source="runtime_reconciled_flat",
            position=SimpleNamespace(pos_qty=0.0),
        )
    )
    truth.effective_open_orders = AsyncMock(
        return_value=SimpleNamespace(
            source="runtime_reconciled_truth",
            orders=[],
        )
    )
    sink = InMemoryStateTransitionRecorder()
    ctl = FlattenController(pg=pg, order_truth_service=truth, transition_sink=sink)

    changed = await ctl.reconcile_confirmed_if_flat(
        order_row={
            "order_id": "33333333-3333-3333-3333-333333333333",
            "strategy_id": "qqq_intraday_001",
            "symbol": "SQQQ",
            "note": '{"flatten_lifecycle":{"state":"pending"}}',
        },
        ib_open_symbols=set(),
    )

    assert changed is True
    assert sink.events()[-1].after_state == "confirmed"
    pg.fetchone.assert_not_called()
