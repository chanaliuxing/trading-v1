from __future__ import annotations

from strategies.stocks_kernel.execution.order_identity import parse_order_ref
from strategies.stocks_kernel.types import BrokerOpenOrder, BrokerPosition, BrokerSyncReport, KernelExecutionEvent

from .kernel import EquityExecutionKernel


def scan_broker_sync(
    *,
    strategy_id: str,
    kernel: EquityExecutionKernel,
    positions: list[BrokerPosition],
    open_orders: list[BrokerOpenOrder],
) -> BrokerSyncReport:
    foreign_positions = []
    for position in positions:
        symbol = str(position.symbol or "").strip().upper()
        strategy_view = kernel.exposure(strategy_id=strategy_id, symbol=symbol)
        if strategy_view.open_lot_count == 0 and abs(float(position.qty or 0.0)) > 0:
            foreign_positions.append(position)

    foreign_orders = []
    for order in open_orders:
        parsed = parse_order_ref(order.order_ref)
        if parsed is None or parsed.strategy_id != str(strategy_id).strip():
            foreign_orders.append(order)
            continue
        binding = kernel.bindings.resolve(
            order_ref=order.order_ref,
            broker_order_id=order.broker_order_id,
            perm_id=order.perm_id,
        )
        if binding is None:
            foreign_orders.append(order)

    ok = not foreign_positions and not foreign_orders
    reason = "ready" if ok else "foreign_broker_state_detected"
    return BrokerSyncReport(
        ok=ok,
        reason=reason,
        foreign_positions=tuple(foreign_positions),
        foreign_orders=tuple(foreign_orders),
        payload={"foreign_position_count": len(foreign_positions), "foreign_order_count": len(foreign_orders)},
    )


def build_broker_sync_events(*, strategy_id: str, report: BrokerSyncReport) -> list[KernelExecutionEvent]:
    events: list[KernelExecutionEvent] = []
    for position in report.foreign_positions:
        events.append(
            KernelExecutionEvent(
                domain="recon",
                component="broker_sync",
                severity="warning",
                status="foreign_position",
                stage="foreign_position_detected",
                strategy_id=str(strategy_id),
                symbol=str(position.symbol).strip().upper(),
                reason=report.reason,
                payload={"qty": float(position.qty or 0.0), "avg_price": position.avg_price},
            )
        )
    for order in report.foreign_orders:
        events.append(
            KernelExecutionEvent(
                domain="recon",
                component="broker_sync",
                severity="warning",
                status="foreign_order",
                stage="foreign_order_detected",
                strategy_id=str(strategy_id),
                symbol=str(order.symbol).strip().upper(),
                order_ref=order.order_ref,
                broker_order_id=order.broker_order_id,
                perm_id=order.perm_id,
                reason=report.reason,
            )
        )
    return events
