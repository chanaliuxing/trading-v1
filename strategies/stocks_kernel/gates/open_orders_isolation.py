from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class OpenOrdersIsolationGate(Gate):
    gate_id = "open_orders_isolation"

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        same_strategy_pending = int(feature_context.snapshot.extras.get("same_strategy_pending_orders") or 0)
        detail = feature_context.snapshot.extras.get("same_strategy_pending_orders_detail")
        allowed = same_strategy_pending == 0
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason="ready" if allowed else "entry_isolation_blocked_same_strategy_pending_order",
            trader_reason_code="open_orders_isolation",
            payload={
                "same_strategy_pending_orders": same_strategy_pending,
                "same_strategy_pending_orders_detail": detail,
            },
        )
