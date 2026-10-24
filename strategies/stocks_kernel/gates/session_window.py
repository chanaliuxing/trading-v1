from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision

from .base import Gate


class SessionWindowGate(Gate):
    gate_id = "session_window"

    def __init__(self, *, allowed_sessions: list[str]) -> None:
        self._allowed = {str(item).strip().upper() for item in allowed_sessions if str(item).strip()}

    def evaluate(self, *, symbol: str, feature_context: FeatureContext, readiness: DataReadinessReport) -> GateDecision:
        session = str(feature_context.snapshot.session or "").strip().upper()
        allowed = bool(session) and session in self._allowed
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason="ready" if allowed else "outside_session_window",
            trader_reason_code="session_window",
            payload={"session": session, "allowed_sessions": sorted(self._allowed)},
        )
