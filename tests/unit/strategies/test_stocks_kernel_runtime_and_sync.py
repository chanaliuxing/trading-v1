from strategies.stocks_kernel.execution.broker_sync import build_broker_sync_events, scan_broker_sync
from strategies.stocks_kernel.execution.kernel import EquityExecutionKernel
from strategies.stocks_kernel.execution.reconciler import build_reconcile_events, reconcile_open_orders
from strategies.stocks_kernel.runtime.recovery import recover_kernel_from_snapshot
from strategies.stocks_kernel.runtime.snapshot import build_runtime_snapshot
from strategies.stocks_kernel.types import BrokerOpenOrder, BrokerPosition, KernelFillReport, PositionLotStatus


def test_runtime_snapshot_and_recovery_preserve_lot_and_bindings():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    submit = kernel.open_entry_lot(strategy_id="alpha", symbol="QQQ", side="LONG", decision_id="d1", trigger_id="t1")
    kernel.acknowledge_order(order_ref=submit.order_ref, broker_order_id="1001", perm_id=101)
    kernel.apply_fill(KernelFillReport(symbol="QQQ", order_ref=submit.order_ref, broker_order_id="1001", perm_id=101, qty=100, price=500.0))
    stop = kernel.register_stop_order(position_lot_id=submit.position_lot_id, strategy_id="alpha", symbol="QQQ", trigger_id="t1", broker_order_id="3001", perm_id=301)

    snapshot = build_runtime_snapshot(strategy_id="alpha", kernel=kernel)
    recovered = recover_kernel_from_snapshot(snapshot)
    lot = recovered.ledger.get_lot(submit.position_lot_id)
    chain = recovered.protection_chain(position_lot_id=submit.position_lot_id)

    assert lot is not None and lot.state == PositionLotStatus.LIVE
    assert recovered.bindings.resolve(order_ref=stop.order_ref) is not None
    assert chain.stop_order_refs == (stop.order_ref,)


def test_broker_sync_and_reconcile_detect_foreign_state():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    submit = kernel.open_entry_lot(strategy_id="alpha", symbol="QQQ", side="LONG", decision_id="d1", trigger_id="t1")
    kernel.acknowledge_order(order_ref=submit.order_ref, broker_order_id="1001", perm_id=101)

    sync = scan_broker_sync(
        strategy_id="alpha",
        kernel=kernel,
        positions=[BrokerPosition(symbol="SPY", qty=10)],
        open_orders=[
            BrokerOpenOrder(symbol="QQQ", order_ref=submit.order_ref, broker_order_id="1001", perm_id=101),
            BrokerOpenOrder(symbol="QQQ", order_ref="stkv1:beta:QQQ:ENTRY:LOT:foreign", broker_order_id="9999", perm_id=999),
        ],
    )
    reconcile = reconcile_open_orders(
        kernel=kernel,
        open_orders=[
            BrokerOpenOrder(symbol="QQQ", order_ref=submit.order_ref, broker_order_id="1001", perm_id=101),
            BrokerOpenOrder(symbol="QQQ", order_ref="stkv1:beta:QQQ:ENTRY:LOT:foreign", broker_order_id="9999", perm_id=999),
        ],
    )

    assert sync.ok is False
    assert len(sync.foreign_positions) == 1
    assert len(sync.foreign_orders) == 1
    assert reconcile.orphan_order_refs == ("stkv1:beta:QQQ:ENTRY:LOT:foreign",)
    sync_events = build_broker_sync_events(strategy_id="alpha", report=sync)
    reconcile_events = build_reconcile_events(strategy_id="alpha", report=reconcile)
    assert sync_events[0].domain == "recon"
    assert reconcile_events[0].stage == "orphan_detected"
