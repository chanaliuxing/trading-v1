from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class DataIntegrityGate(Gate):
    gate_id = "data_integrity"

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        allowed = bool(readiness.snapshot_ready and readiness.history_ready)
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason="ready" if allowed else readiness.reason,
            trader_reason_code="data_integrity",
            payload=dict(readiness.payload),
        )
