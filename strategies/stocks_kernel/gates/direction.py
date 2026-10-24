from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class DirectionMatchGate(Gate):
    gate_id = "direction_match"

    def __init__(self, *, feature_id: str, min_abs_value: float = 0.0) -> None:
        self._feature_id = str(feature_id).strip()
        self._min_abs_value = float(min_abs_value)

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        value = feature_context.values.get(self._feature_id)
        if value is None:
            return GateDecision(
                gate_id=self.gate_id,
                allowed=False,
                reason="direction_feature_missing",
                trader_reason_code="direction_match",
                payload={"feature_id": self._feature_id},
            )
        allowed = abs(float(value)) >= self._min_abs_value
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason="ready" if allowed else "direction_below_threshold",
            trader_reason_code="direction_match",
            payload={"feature_id": self._feature_id, "value": float(value), "min_abs_value": self._min_abs_value},
        )
