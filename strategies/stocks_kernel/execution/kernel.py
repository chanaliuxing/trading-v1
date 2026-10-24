from __future__ import annotations

import uuid
from typing import Optional

from strategies.stocks_kernel.types import (
    KernelExecutionEvent,
    KernelFillApplyResult,
    KernelFillReport,
    KernelSubmitResult,
    LotOrderBinding,
    LotProtectionChainView,
    PositionLot,
)

from .audit_trail import InMemoryExecutionAuditTrail
from .fill_attribution import FillAttributionEngine
from .lot_ledger import PositionLotLedger
from .order_binding_book import LotOrderBindingBook
from .order_identity import build_order_ref


class EquityExecutionKernel:
    """
    Lot-level execution state for the new stocks kernel.

    This class is intentionally self-contained so it can be tested before
    wiring into any live strategy or broker service.
    """

    def __init__(self, *, order_namespace: str) -> None:
        self.order_namespace = str(order_namespace or "").strip() or "stkv1"
        self.ledger = PositionLotLedger()
        self.bindings = LotOrderBindingBook()
        self.fill_attribution = FillAttributionEngine(self.ledger)
        self.audit = InMemoryExecutionAuditTrail()
        self.strategy_run_id: str | None = None

    def set_strategy_run_id(self, strategy_run_id: str | None) -> None:
        self.strategy_run_id = str(strategy_run_id or "").strip() or None

    def open_entry_lot(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        decision_id: str,
        lane_id: Optional[str] = None,
        ownership_scope: str = "strategy",
        flatten_authority: str = "strategy",
        protection_authority: str = "strategy",
        adopt_authority: str = "strategy",
        trigger_id: Optional[str] = None,
    ) -> KernelSubmitResult:
        symbol_v = str(symbol or "").strip().upper()
        strategy_id_v = str(strategy_id or "").strip()
        side_v = str(side or "").strip().upper()
        if not strategy_id_v or not symbol_v or side_v not in {"LONG", "SHORT"}:
            raise ValueError("strategy_id/symbol/side are required")
        lot_id = uuid.uuid4().hex
        order_ref = build_order_ref(
            namespace=self.order_namespace,
            strategy_id=strategy_id_v,
            symbol=symbol_v,
            kind="ENTRY",
            lane_id=lane_id,
            trigger_id=trigger_id,
            lot_id=lot_id,
        )
        self.ledger.open_lot(
            PositionLot(
                position_lot_id=lot_id,
                strategy_id=strategy_id_v,
                symbol=symbol_v,
                side=side_v,
                opened_by_decision_id=str(decision_id or "").strip(),
                opened_by_trigger_id=str(trigger_id or "").strip() or None,
                entry_order_ref=order_ref,
                lane_id=str(lane_id or "").strip() or None,
                ownership_scope=str(ownership_scope or "strategy").strip() or "strategy",
                flatten_authority=str(flatten_authority or "strategy").strip() or "strategy",
                protection_authority=str(protection_authority or "strategy").strip() or "strategy",
                adopt_authority=str(adopt_authority or "strategy").strip() or "strategy",
            )
        )
        self.bindings.bind(
            LotOrderBinding(
                position_lot_id=lot_id,
                strategy_id=strategy_id_v,
                symbol=symbol_v,
                order_role="ENTRY",
                order_ref=order_ref,
                lane_id=str(lane_id or "").strip() or None,
                ownership_scope=str(ownership_scope or "strategy").strip() or "strategy",
            )
        )
        self.audit.record(
            KernelExecutionEvent(
                stage="entry_lot_opened",
                strategy_id=strategy_id_v,
                symbol=symbol_v,
                domain="exec",
                component="execution_kernel",
                strategy_run_id=self.strategy_run_id,
                trigger_id=str(trigger_id or "").strip() or None,
                decision_id=str(decision_id or "").strip() or None,
                trace_id=str(decision_id or "").strip() or None,
                position_lot_id=lot_id,
                lane_id=str(lane_id or "").strip() or None,
                order_ref=order_ref,
                order_role="ENTRY",
                ownership_scope=str(ownership_scope or "strategy").strip() or "strategy",
                reason="entry_intent_created",
            )
        )
        return KernelSubmitResult(
            position_lot_id=lot_id,
            order_ref=order_ref,
            strategy_id=strategy_id_v,
            symbol=symbol_v,
            side=side_v,
            lane_id=str(lane_id or "").strip() or None,
        )

    def acknowledge_order(
        self,
        *,
        order_ref: str,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
    ) -> LotOrderBinding:
        binding = self.bindings.attach_broker_identity(
            order_ref=order_ref,
            broker_order_id=broker_order_id,
            perm_id=perm_id,
        )
        if binding.order_role == "ENTRY":
            self.ledger.attach_entry_identity(
                position_lot_id=binding.position_lot_id,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
            )
        self.audit.record(
            KernelExecutionEvent(
                stage="order_acknowledged",
                strategy_id=binding.strategy_id,
                symbol=binding.symbol,
                domain="exec",
                component="execution_kernel",
                strategy_run_id=self.strategy_run_id,
                position_lot_id=binding.position_lot_id,
                order_ref=binding.order_ref,
                order_role=binding.order_role,
                broker_order_id=binding.broker_order_id,
                perm_id=binding.perm_id,
                reason="broker_identity_attached",
            )
        )
        return binding

    def register_stop_order(
        self,
        *,
        position_lot_id: str,
        strategy_id: str,
        symbol: str,
        lane_id: Optional[str] = None,
        ownership_scope: str = "strategy",
        trigger_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
    ) -> LotOrderBinding:
        order_ref = build_order_ref(
            namespace=self.order_namespace,
            strategy_id=strategy_id,
            symbol=symbol,
            kind="STOP",
            lane_id=lane_id,
            trigger_id=trigger_id,
            lot_id=position_lot_id,
        )
        binding = self.bindings.bind(
            LotOrderBinding(
                position_lot_id=position_lot_id,
                strategy_id=strategy_id,
                symbol=str(symbol).strip().upper(),
                order_role="STOP",
                order_ref=order_ref,
                lane_id=str(lane_id or "").strip() or None,
                ownership_scope=str(ownership_scope or "strategy").strip() or "strategy",
                broker_order_id=broker_order_id,
                perm_id=perm_id,
            )
        )
        self.ledger.attach_protection_order(position_lot_id=position_lot_id, protection_order_ref=order_ref)
        self.audit.record(
            KernelExecutionEvent(
                stage="stop_registered",
                strategy_id=binding.strategy_id,
                symbol=binding.symbol,
                domain="exec",
                component="execution_kernel",
                strategy_run_id=self.strategy_run_id,
                trigger_id=str(trigger_id or "").strip() or None,
                position_lot_id=binding.position_lot_id,
                lane_id=binding.lane_id,
                order_ref=binding.order_ref,
                order_role=binding.order_role,
                broker_order_id=binding.broker_order_id,
                perm_id=binding.perm_id,
                ownership_scope=binding.ownership_scope,
                reason="lot_stop_bound",
            )
        )
        return binding

    def request_flatten(
        self,
        *,
        position_lot_id: str,
        strategy_id: str,
        symbol: str,
        lane_id: Optional[str] = None,
        ownership_scope: str = "strategy",
        trigger_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
    ) -> LotOrderBinding:
        order_ref = build_order_ref(
            namespace=self.order_namespace,
            strategy_id=strategy_id,
            symbol=symbol,
            kind="FLATTEN",
            lane_id=lane_id,
            trigger_id=trigger_id,
            lot_id=position_lot_id,
        )
        binding = self.bindings.bind(
            LotOrderBinding(
                position_lot_id=position_lot_id,
                strategy_id=strategy_id,
                symbol=str(symbol).strip().upper(),
                order_role="FLATTEN",
                order_ref=order_ref,
                lane_id=str(lane_id or "").strip() or None,
                ownership_scope=str(ownership_scope or "strategy").strip() or "strategy",
                broker_order_id=broker_order_id,
                perm_id=perm_id,
            )
        )
        self.ledger.request_exit(position_lot_id=position_lot_id, flatten_order_ref=order_ref)
        self.audit.record(
            KernelExecutionEvent(
                stage="flatten_requested",
                strategy_id=binding.strategy_id,
                symbol=binding.symbol,
                domain="exec",
                component="execution_kernel",
                strategy_run_id=self.strategy_run_id,
                trigger_id=str(trigger_id or "").strip() or None,
                position_lot_id=binding.position_lot_id,
                lane_id=binding.lane_id,
                order_ref=binding.order_ref,
                order_role=binding.order_role,
                broker_order_id=binding.broker_order_id,
                perm_id=binding.perm_id,
                ownership_scope=binding.ownership_scope,
                reason="lot_flatten_bound",
            )
        )
        return binding

    def apply_fill(self, fill: KernelFillReport) -> KernelFillApplyResult:
        binding = self.bindings.resolve(
            perm_id=fill.perm_id,
            broker_order_id=fill.broker_order_id,
            order_ref=fill.order_ref,
        )
        if binding is None:
            attribution = self.fill_attribution.attribute_fill(fill)
            self.audit.record(
                KernelExecutionEvent(
                    stage="fill_quarantined" if attribution.quarantine_required else "fill_unbound",
                    strategy_id=attribution.strategy_id,
                    symbol=str(fill.symbol or "").strip().upper(),
                    domain="exec",
                    component="execution_kernel",
                    severity="warning" if attribution.quarantine_required else "info",
                    status="quarantined" if attribution.quarantine_required else "ignored",
                    strategy_run_id=self.strategy_run_id,
                    position_lot_id=attribution.position_lot_id,
                    order_ref=fill.order_ref,
                    broker_order_id=fill.broker_order_id,
                    perm_id=fill.perm_id,
                    trace_id=str(fill.exec_id or fill.order_ref or "") or None,
                    reason=attribution.reason,
                )
            )
            return KernelFillApplyResult(
                position_lot_id=attribution.position_lot_id,
                strategy_id=attribution.strategy_id,
                symbol=str(fill.symbol or "").strip().upper(),
                order_role=None,
                action="quarantine" if attribution.quarantine_required else "ignored",
                reason=attribution.reason,
                quarantine_required=attribution.quarantine_required,
            )

        if binding.order_role == "ENTRY":
            self.ledger.record_entry_fill(
                position_lot_id=binding.position_lot_id,
                fill_qty=float(fill.qty or 0.0),
                fill_price=float(fill.price or 0.0),
                broker_order_id=binding.broker_order_id or fill.broker_order_id,
                perm_id=binding.perm_id if binding.perm_id is not None else fill.perm_id,
            )
            self.audit.record(
                KernelExecutionEvent(
                    stage="entry_fill_applied",
                    strategy_id=binding.strategy_id,
                    symbol=binding.symbol,
                    domain="exec",
                    component="execution_kernel",
                    status="applied",
                    strategy_run_id=self.strategy_run_id,
                    position_lot_id=binding.position_lot_id,
                    order_ref=binding.order_ref,
                    order_role=binding.order_role,
                    broker_order_id=binding.broker_order_id or fill.broker_order_id,
                    perm_id=binding.perm_id if binding.perm_id is not None else fill.perm_id,
                    trace_id=str(fill.exec_id or fill.order_ref or "") or None,
                    reason="bound_entry_fill",
                    payload={"qty": float(fill.qty or 0.0), "price": float(fill.price or 0.0)},
                )
            )
            return KernelFillApplyResult(
                position_lot_id=binding.position_lot_id,
                strategy_id=binding.strategy_id,
                symbol=binding.symbol,
                order_role=binding.order_role,
                action="entry_fill_applied",
                reason="bound_entry_fill",
            )

        if binding.order_role in {"STOP", "FLATTEN"}:
            self.ledger.record_exit_fill(
                position_lot_id=binding.position_lot_id,
                fill_qty=float(fill.qty or 0.0),
            )
            self.audit.record(
                KernelExecutionEvent(
                    stage="exit_fill_applied",
                    strategy_id=binding.strategy_id,
                    symbol=binding.symbol,
                    domain="exec",
                    component="execution_kernel",
                    status="applied",
                    strategy_run_id=self.strategy_run_id,
                    position_lot_id=binding.position_lot_id,
                    order_ref=binding.order_ref,
                    order_role=binding.order_role,
                    broker_order_id=binding.broker_order_id or fill.broker_order_id,
                    perm_id=binding.perm_id if binding.perm_id is not None else fill.perm_id,
                    trace_id=str(fill.exec_id or fill.order_ref or "") or None,
                    reason=f"bound_{binding.order_role.lower()}_fill",
                    payload={"qty": float(fill.qty or 0.0), "price": float(fill.price or 0.0)},
                )
            )
            return KernelFillApplyResult(
                position_lot_id=binding.position_lot_id,
                strategy_id=binding.strategy_id,
                symbol=binding.symbol,
                order_role=binding.order_role,
                action="exit_fill_applied",
                reason=f"bound_{binding.order_role.lower()}_fill",
            )

        return KernelFillApplyResult(
            position_lot_id=binding.position_lot_id,
            strategy_id=binding.strategy_id,
            symbol=binding.symbol,
            order_role=binding.order_role,
            action="quarantine",
            reason="unsupported_order_role",
            quarantine_required=True,
        )

    def exposure(self, *, strategy_id: str, symbol: str):
        return self.ledger.symbol_view(strategy_id=strategy_id, symbol=symbol)

    def protection_chain(self, *, position_lot_id: str) -> LotProtectionChainView:
        return self.bindings.protection_chain(position_lot_id=position_lot_id)
