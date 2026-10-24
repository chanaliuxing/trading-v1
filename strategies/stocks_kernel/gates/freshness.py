from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class FreshnessGate(Gate):
    gate_id = "freshness"

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        return GateDecision(
            gate_id=self.gate_id,
            allowed=bool(readiness.freshness_ok),
            reason="ready" if readiness.freshness_ok else "snapshot_stale",
            trader_reason_code="freshness",
            payload=dict(readiness.payload),
        )
