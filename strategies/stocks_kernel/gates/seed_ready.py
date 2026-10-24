from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class SeedReadyGate(Gate):
    gate_id = "seed_ready"

    def __init__(self, *, required_features: list[str]) -> None:
        self._required = [str(item).strip() for item in required_features if str(item).strip()]

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        missing = [fid for fid in self._required if feature_context.values.get(fid) is None]
        allowed = not missing
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason="ready" if allowed else "feature_seed_missing",
            trader_reason_code="seed_ready",
            payload={"missing_features": missing},
        )
