from strategies.stocks_kernel.modules.order_lifecycle import OrderLifecycleModule
import asyncio

from services.strategy_runtime_v2 import InMemoryStateTransitionRecorder, order_transition_targets


def test_order_lifecycle_breach_freezes_symbol():
    module = OrderLifecycleModule(pending_timeout_sec=60.0)
    module.register_created(
        order_ref="stkv1:alpha:SOXL:ENTRY:lot-1",
        symbol="SOXL",
        position_lot_id="lot-1",
        now_ts_epoch_s=100.0,
    )
    module.mark_submitted(order_ref="stkv1:alpha:SOXL:ENTRY:lot-1", now_ts_epoch_s=101.0)
    module.mark_accepted(order_ref="stkv1:alpha:SOXL:ENTRY:lot-1", now_ts_epoch_s=102.0, broker_order_id="11", perm_id=22)

    breaches = module.sweep_breaches(now_ts_epoch_s=163.0)

    assert len(breaches) == 1
    assert breaches[0].reason == "ack_not_converged_timeout"
    assert module.should_freeze_entry(symbol="SOXL") is True
    assert module.freeze_reason(symbol="SOXL") == "ack_not_converged_timeout"


def test_order_lifecycle_fill_clears_pending_breach_path():
    module = OrderLifecycleModule(pending_timeout_sec=60.0)
    module.register_created(
        order_ref="stkv1:alpha:SOXS:ENTRY:lot-2",
        symbol="SOXS",
        position_lot_id="lot-2",
        now_ts_epoch_s=100.0,
    )
    module.mark_accepted(order_ref="stkv1:alpha:SOXS:ENTRY:lot-2", now_ts_epoch_s=101.0)
    module.mark_fill(order_ref="stkv1:alpha:SOXS:ENTRY:lot-2", now_ts_epoch_s=110.0)

    breaches = module.sweep_breaches(now_ts_epoch_s=200.0)

    assert breaches == ()
    assert module.should_freeze_entry(symbol="SOXS") is False


def test_order_lifecycle_exposes_non_terminal_records_for_truth_reconcile():
    module = OrderLifecycleModule(pending_timeout_sec=60.0)
    module.register_created(
        order_ref="stkv1:alpha:NVDA:ENTRY:lot-3",
        symbol="NVDA",
        position_lot_id="lot-3",
        now_ts_epoch_s=100.0,
    )
    module.mark_accepted(order_ref="stkv1:alpha:NVDA:ENTRY:lot-3", now_ts_epoch_s=101.0)
    module.register_created(
        order_ref="stkv1:alpha:SMH:ENTRY:lot-4",
        symbol="SMH",
        position_lot_id="lot-4",
        now_ts_epoch_s=100.0,
    )
    module.mark_fill(order_ref="stkv1:alpha:SMH:ENTRY:lot-4", now_ts_epoch_s=101.0)

    records = module.non_terminal_records(symbols=["NVDA", "SMH"])

    assert [record.order_ref for record in records] == ["stkv1:alpha:NVDA:ENTRY:lot-3"]


def test_order_lifecycle_shadow_events_follow_order_contract():
    sink = InMemoryStateTransitionRecorder()
    module = OrderLifecycleModule(pending_timeout_sec=60.0, transition_sink=sink)
    module.register_created(
        order_ref="stkv1:alpha:NVDA:ENTRY:lot-3",
        symbol="NVDA",
        position_lot_id="lot-3",
        now_ts_epoch_s=100.0,
    )
    module.mark_submitted(order_ref="stkv1:alpha:NVDA:ENTRY:lot-3", now_ts_epoch_s=101.0)
    module.mark_accepted(order_ref="stkv1:alpha:NVDA:ENTRY:lot-3", now_ts_epoch_s=102.0, broker_order_id="77", perm_id=88)
    module.mark_fill(order_ref="stkv1:alpha:NVDA:ENTRY:lot-3", now_ts_epoch_s=103.0)

    asyncio.run(asyncio.sleep(0))
    events = sink.events()
    assert [event.after_state for event in events] == ["submitted", "accepted", "filled"]
    assert [event.state_machine for event in events] == ["order", "order", "order"]


def test_order_lifecycle_terminal_shadow_event_records_cancelled():
    sink = InMemoryStateTransitionRecorder()
    module = OrderLifecycleModule(pending_timeout_sec=60.0, transition_sink=sink, strategy_id="alpha_strategy")
    module.register_created(
        order_ref="stkv1:alpha:AMD:ENTRY:lot-4",
        symbol="AMD",
        position_lot_id="lot-4",
        now_ts_epoch_s=100.0,
    )
    module.mark_submitted(order_ref="stkv1:alpha:AMD:ENTRY:lot-4", now_ts_epoch_s=101.0)
    module.mark_terminal(order_ref="stkv1:alpha:AMD:ENTRY:lot-4", state="CANCELLED", now_ts_epoch_s=102.0)

    asyncio.run(asyncio.sleep(0))
    events = sink.events()
    assert [event.after_state for event in events] == ["submitted", "cancelled"]
    assert all(event.strategy_id == "alpha_strategy" for event in events)
    assert set(order_transition_targets("accepted")) == {"cancelled", "expired", "failed", "filled", "rejected"}
