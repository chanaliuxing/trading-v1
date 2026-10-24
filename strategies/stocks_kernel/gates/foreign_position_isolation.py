from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class ForeignPositionIsolationGate(Gate):
    gate_id = "foreign_position_isolation"

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        foreign_qty = float(feature_context.snapshot.extras.get("foreign_position_qty") or 0.0)
        allowed = abs(foreign_qty) <= 1e-12
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason="ready" if allowed else "foreign_position_detected",
            trader_reason_code="foreign_position_isolation",
            payload={"foreign_position_qty": foreign_qty},
        )
