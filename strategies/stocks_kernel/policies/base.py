from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from strategies.stocks_kernel.types import (
    ActionPlan,
    ActionType,
    EntryPlan,
    ExitPlan,
    FeatureContext,
    GateEvaluationReport,
    SignalIntent,
)


@dataclass(frozen=True)
class EntryPermissionDecision:
    allowed: bool
    reason: str = "allowed"
    payload: dict[str, Any] = field(default_factory=dict)


class Policy(ABC):
    policy_id: str

    @abstractmethod
    def build_signal(self, *, symbol: str, feature_context: FeatureContext, gate_report: GateEvaluationReport) -> SignalIntent:
        raise NotImplementedError

    @abstractmethod
    def build_entry_plan(self, *, signal: SignalIntent, feature_context: FeatureContext) -> EntryPlan | None:
        raise NotImplementedError

    def build_add_plan(self, *, signal: SignalIntent, feature_context: FeatureContext) -> EntryPlan | None:
        return None

    def build_stop_plan(
        self,
        *,
        signal: SignalIntent,
        entry_plan: EntryPlan | None,
        feature_context: FeatureContext,
    ) -> ExitPlan | None:
        return None

    def build_take_profit_plan(
        self,
        *,
        signal: SignalIntent,
        entry_plan: EntryPlan | None,
        feature_context: FeatureContext,
    ) -> ExitPlan | None:
        return None

    def build_retreat_plan(
        self,
        *,
        signal: SignalIntent,
        feature_context: FeatureContext,
    ) -> ExitPlan | None:
        return None

    def build_exit_plans(self, *, feature_context: FeatureContext) -> list[ExitPlan]:
        return []

    def build_action_plans(
        self,
        *,
        signal: SignalIntent,
        feature_context: FeatureContext,
    ) -> list[ActionPlan]:
        """
        Preferred extensible contract for pluggable strategies.

        Default implementation preserves backward compatibility by translating
        existing entry/exit plan APIs into ordered action plans.
        """
        plans: list[ActionPlan] = []
        entry_plan = self.build_entry_plan(signal=signal, feature_context=feature_context)
        add_plan = self.build_add_plan(signal=signal, feature_context=feature_context)
        if entry_plan is not None:
            plans.append(
                ActionPlan(
                    action_type=ActionType.ENTRY,
                    symbol=entry_plan.symbol,
                    side=entry_plan.side,
                    qty=float(entry_plan.qty),
                    trigger_id=entry_plan.trigger_id,
                    reason=str(entry_plan.payload.get("reason") or signal.action or "entry"),
                    payload=dict(entry_plan.payload),
                )
            )
        if add_plan is not None:
            plans.append(
                ActionPlan(
                    action_type=ActionType.ADD,
                    symbol=add_plan.symbol,
                    side=add_plan.side,
                    qty=float(add_plan.qty),
                    trigger_id=add_plan.trigger_id,
                    reason=str(add_plan.payload.get("reason") or "add"),
                    payload=dict(add_plan.payload),
                )
            )
        for exit_plan in list(self.build_exit_plans(feature_context=feature_context) or []):
            if exit_plan is None:
                continue
            order_role = str(getattr(exit_plan, "order_role", "") or "").strip().upper()
            action_type = self._action_type_from_order_role(order_role)
            if action_type is None:
                continue
            plans.append(
                ActionPlan(
                    action_type=action_type,
                    symbol=str(getattr(exit_plan, "symbol", signal.symbol) or signal.symbol),
                    qty=float(getattr(exit_plan, "qty", 0.0) or 0.0),
                    position_lot_id=getattr(exit_plan, "position_lot_id", None),
                    close_only=True,
                    flatten_action=action_type == ActionType.FLATTEN,
                    reduce_only=True,
                    reason=str(getattr(exit_plan, "payload", {}).get("reason") or order_role.lower()),
                    payload=dict(getattr(exit_plan, "payload", {}) or {}),
                )
            )
        return plans

    @staticmethod
    def _action_type_from_order_role(order_role: str) -> str | None:
        role = str(order_role or "").strip().upper()
        if role in {"STOP"}:
            return ActionType.STOP
        if role in {"TAKE_PROFIT", "TP1", "TP"}:
            return ActionType.TAKE_PROFIT
        if role in {"RETREAT"}:
            return ActionType.RETREAT
        if role in {"REDUCE"}:
            return ActionType.REDUCE
        if role in {"FLATTEN", "EXIT", "MANUAL_FLATTEN"}:
            return ActionType.FLATTEN
        return None

    def on_fill_feedback(
        self,
        *,
        position_lot_id: str,
        order_role: str,
        filled_qty: float,
        fill_price: float | None,
        feature_context: FeatureContext | None = None,
    ) -> dict[str, Any] | None:
        return None

    def evaluate_entry_permission(
        self,
        *,
        signal: SignalIntent,
        feature_context: FeatureContext,
        runtime_state: dict[str, Any],
    ) -> EntryPermissionDecision:
        return EntryPermissionDecision(allowed=True)
