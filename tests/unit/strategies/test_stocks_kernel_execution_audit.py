from strategies.stocks_kernel.execution.kernel import EquityExecutionKernel
from strategies.stocks_kernel.types import KernelFillReport


def test_execution_kernel_records_lot_scoped_audit_chain():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    submit = kernel.open_entry_lot(
        strategy_id="alpha",
        symbol="QQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )
    kernel.acknowledge_order(order_ref=submit.order_ref, broker_order_id="1001", perm_id=101)
    kernel.register_stop_order(
        position_lot_id=submit.position_lot_id,
        strategy_id="alpha",
        symbol="QQQ",
        trigger_id="t1",
        broker_order_id="3001",
        perm_id=301,
    )
    kernel.apply_fill(
        KernelFillReport(
            symbol="QQQ",
            order_ref=submit.order_ref,
            broker_order_id="1001",
            perm_id=101,
            qty=100,
            price=500.0,
        )
    )

    events = kernel.audit.list_events(position_lot_id=submit.position_lot_id)
    stages = [event.stage for event in events]

    assert stages == [
        "entry_lot_opened",
        "order_acknowledged",
        "stop_registered",
        "entry_fill_applied",
    ]
    assert all(event.position_lot_id == submit.position_lot_id for event in events)


def test_execution_kernel_records_quarantine_event_for_unattributed_fill():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    kernel.open_entry_lot(
        strategy_id="alpha",
        symbol="QQQ",
        side="LONG",
        decision_id="d1",
        trigger_id="t1",
    )

    kernel.apply_fill(
        KernelFillReport(
            symbol="QQQ",
            broker_order_id="9999",
            perm_id=999,
            qty=100,
            price=500.0,
        )
    )

    events = kernel.audit.list_events(symbol="QQQ")

    assert events[-1].stage == "fill_quarantined"
    assert events[-1].reason == "fill_unattributed"
