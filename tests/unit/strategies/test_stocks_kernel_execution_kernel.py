from strategies.stocks_kernel.execution.kernel import EquityExecutionKernel
from strategies.stocks_kernel.types import KernelFillReport, PositionLotStatus


def test_execution_kernel_routes_same_symbol_multi_strategy_entry_fills_by_bound_identity():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    alpha = kernel.open_entry_lot(
        strategy_id="alpha",
        symbol="QQQ",
        side="LONG",
        decision_id="d-alpha",
        trigger_id="t-alpha",
    )
    beta = kernel.open_entry_lot(
        strategy_id="beta",
        symbol="QQQ",
        side="LONG",
        decision_id="d-beta",
        trigger_id="t-beta",
    )

    kernel.acknowledge_order(order_ref=alpha.order_ref, broker_order_id="1001", perm_id=101)
    kernel.acknowledge_order(order_ref=beta.order_ref, broker_order_id="1002", perm_id=202)

    result = kernel.apply_fill(
        KernelFillReport(symbol="QQQ", broker_order_id="1002", perm_id=202, order_ref=beta.order_ref, qty=50, price=500.5)
    )

    alpha_lot = kernel.ledger.get_lot(alpha.position_lot_id)
    beta_lot = kernel.ledger.get_lot(beta.position_lot_id)

    assert result.action == "entry_fill_applied"
    assert result.position_lot_id == beta.position_lot_id
    assert alpha_lot is not None and alpha_lot.live_qty == 0.0
    assert beta_lot is not None and beta_lot.live_qty == 50.0
    assert beta_lot.state == PositionLotStatus.LIVE


def test_execution_kernel_stop_fill_only_closes_target_lot():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    alpha = kernel.open_entry_lot(strategy_id="alpha", symbol="QQQ", side="LONG", decision_id="d1", trigger_id="t1")
    beta = kernel.open_entry_lot(strategy_id="beta", symbol="QQQ", side="LONG", decision_id="d2", trigger_id="t2")
    kernel.acknowledge_order(order_ref=alpha.order_ref, broker_order_id="1001", perm_id=101)
    kernel.acknowledge_order(order_ref=beta.order_ref, broker_order_id="1002", perm_id=202)
    kernel.apply_fill(KernelFillReport(symbol="QQQ", order_ref=alpha.order_ref, broker_order_id="1001", perm_id=101, qty=100, price=500.0))
    kernel.apply_fill(KernelFillReport(symbol="QQQ", order_ref=beta.order_ref, broker_order_id="1002", perm_id=202, qty=100, price=501.0))

    stop = kernel.register_stop_order(
        position_lot_id=alpha.position_lot_id,
        strategy_id="alpha",
        symbol="QQQ",
        trigger_id="t1",
        broker_order_id="3001",
        perm_id=301,
    )
    result = kernel.apply_fill(
        KernelFillReport(symbol="QQQ", order_ref=stop.order_ref, broker_order_id="3001", perm_id=301, qty=100, price=498.0)
    )

    alpha_lot = kernel.ledger.get_lot(alpha.position_lot_id)
    beta_lot = kernel.ledger.get_lot(beta.position_lot_id)

    assert result.action == "exit_fill_applied"
    assert result.position_lot_id == alpha.position_lot_id
    assert alpha_lot is not None and alpha_lot.state == PositionLotStatus.CLOSED
    assert beta_lot is not None and beta_lot.state == PositionLotStatus.LIVE
    assert beta_lot.live_qty == 100.0


def test_execution_kernel_builds_one_lot_one_protection_chain():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    alpha = kernel.open_entry_lot(strategy_id="alpha", symbol="QQQ", side="LONG", decision_id="d1", trigger_id="t1")
    kernel.acknowledge_order(order_ref=alpha.order_ref, broker_order_id="1001", perm_id=101)
    kernel.register_stop_order(
        position_lot_id=alpha.position_lot_id,
        strategy_id="alpha",
        symbol="QQQ",
        trigger_id="t1",
        broker_order_id="3001",
        perm_id=301,
    )
    flatten = kernel.request_flatten(
        position_lot_id=alpha.position_lot_id,
        strategy_id="alpha",
        symbol="QQQ",
        trigger_id="t1",
        broker_order_id="4001",
        perm_id=401,
    )

    chain = kernel.protection_chain(position_lot_id=alpha.position_lot_id)

    assert chain.entry_order_ref == alpha.order_ref
    assert len(chain.stop_order_refs) == 1
    assert chain.flatten_order_refs == (flatten.order_ref,)


def test_execution_kernel_unattributed_fill_quarantines_without_state_mutation():
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    alpha = kernel.open_entry_lot(strategy_id="alpha", symbol="QQQ", side="LONG", decision_id="d1", trigger_id="t1")

    result = kernel.apply_fill(
        KernelFillReport(symbol="QQQ", broker_order_id="9999", perm_id=999, qty=100, price=500.0)
    )
    alpha_lot = kernel.ledger.get_lot(alpha.position_lot_id)

    assert result.action == "quarantine"
    assert result.quarantine_required is True
    assert alpha_lot is not None and alpha_lot.state == PositionLotStatus.PENDING_ENTRY
    assert alpha_lot.live_qty == 0.0
