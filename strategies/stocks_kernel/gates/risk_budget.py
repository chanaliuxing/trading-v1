from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class RiskBudgetGate(Gate):
    gate_id = "risk_budget"

    def __init__(self, *, max_notional: float) -> None:
        self._max_notional = float(max_notional)

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        current_notional = float(feature_context.snapshot.extras.get("current_notional") or 0.0)
        planned_notional = float(feature_context.snapshot.extras.get("planned_notional") or 0.0)
        total = current_notional + planned_notional
        allowed = total <= self._max_notional
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason="ready" if allowed else "risk_budget_exceeded",
            trader_reason_code="risk_budget",
            payload={"current_notional": current_notional, "planned_notional": planned_notional, "max_notional": self._max_notional},
        )
