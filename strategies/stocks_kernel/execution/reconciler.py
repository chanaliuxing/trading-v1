from __future__ import annotations

from dataclasses import dataclass

from strategies.stocks_kernel.types import BrokerOpenOrder, KernelExecutionEvent

from .kernel import EquityExecutionKernel


@dataclass(frozen=True)
class ReconcileReport:
    orphan_order_refs: tuple[str, ...]
    unresolved_order_refs: tuple[str, ...]


def reconcile_open_orders(*, kernel: EquityExecutionKernel, open_orders: list[BrokerOpenOrder]) -> ReconcileReport:
    orphan_order_refs: list[str] = []
    unresolved_order_refs: list[str] = []
    for order in open_orders:
        binding = kernel.bindings.resolve(
            order_ref=order.order_ref,
            broker_order_id=order.broker_order_id,
            perm_id=order.perm_id,
        )
        if binding is None:
            orphan_order_refs.append(order.order_ref)
            continue
        lot = kernel.ledger.get_lot(binding.position_lot_id)
        if lot is None:
            unresolved_order_refs.append(order.order_ref)
    return ReconcileReport(
        orphan_order_refs=tuple(sorted(orphan_order_refs)),
        unresolved_order_refs=tuple(sorted(unresolved_order_refs)),
    )


def build_reconcile_events(*, strategy_id: str, report: ReconcileReport) -> list[KernelExecutionEvent]:
    events: list[KernelExecutionEvent] = []
    for order_ref in report.orphan_order_refs:
        events.append(
            KernelExecutionEvent(
                domain="recon",
                component="reconciler",
                severity="warning",
                status="orphan",
                stage="orphan_detected",
                strategy_id=str(strategy_id),
                symbol="SYSTEM",
                order_ref=order_ref,
                reason="reconcile_orphan_order",
            )
        )
    for order_ref in report.unresolved_order_refs:
        events.append(
            KernelExecutionEvent(
                domain="recon",
                component="reconciler",
                severity="warning",
                status="unresolved",
                stage="reconcile_unresolved_order",
                strategy_id=str(strategy_id),
                symbol="SYSTEM",
                order_ref=order_ref,
                reason="reconcile_unresolved_order",
            )
        )
    return events
