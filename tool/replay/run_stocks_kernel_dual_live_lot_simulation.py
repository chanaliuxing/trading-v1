from __future__ import annotations

from strategies.stocks_kernel.execution.kernel import EquityExecutionKernel
from strategies.stocks_kernel.types import KernelFillReport


def run_dual_live_lot_simulation() -> dict[str, object]:
    kernel = EquityExecutionKernel(order_namespace="stkv1")
    alpha = kernel.open_entry_lot(strategy_id="alpha", symbol="QQQ", side="LONG", decision_id="d-alpha", trigger_id="t-alpha")
    beta = kernel.open_entry_lot(strategy_id="beta", symbol="QQQ", side="SHORT", decision_id="d-beta", trigger_id="t-beta")
    kernel.acknowledge_order(order_ref=alpha.order_ref, broker_order_id="1001", perm_id=101)
    kernel.acknowledge_order(order_ref=beta.order_ref, broker_order_id="1002", perm_id=202)
    kernel.apply_fill(KernelFillReport(symbol="QQQ", order_ref=alpha.order_ref, broker_order_id="1001", perm_id=101, qty=100, price=500.0))
    kernel.apply_fill(KernelFillReport(symbol="QQQ", order_ref=beta.order_ref, broker_order_id="1002", perm_id=202, qty=50, price=499.5))
    alpha_stop = kernel.register_stop_order(position_lot_id=alpha.position_lot_id, strategy_id="alpha", symbol="QQQ", trigger_id="t-alpha", broker_order_id="3001", perm_id=301)
    kernel.request_flatten(position_lot_id=beta.position_lot_id, strategy_id="beta", symbol="QQQ", trigger_id="t-beta", broker_order_id="4001", perm_id=401)
    kernel.apply_fill(KernelFillReport(symbol="QQQ", order_ref=alpha_stop.order_ref, broker_order_id="3001", perm_id=301, qty=100, price=498.0))
    alpha_lot = kernel.ledger.get_lot(alpha.position_lot_id)
    beta_lot = kernel.ledger.get_lot(beta.position_lot_id)
    return {
        "alpha_state": alpha_lot.state if alpha_lot else None,
        "beta_state": beta_lot.state if beta_lot else None,
        "alpha_live_qty": alpha_lot.live_qty if alpha_lot else None,
        "beta_live_qty": beta_lot.live_qty if beta_lot else None,
        "event_count": len(kernel.audit.list_events(symbol="QQQ")),
    }
