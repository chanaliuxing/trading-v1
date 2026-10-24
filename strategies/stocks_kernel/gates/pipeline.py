from __future__ import annotations

import inspect
from typing import Any

from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateEvaluationReport

from .base import Gate, GateRuntimeContext


class GatePipeline:
    def __init__(self, gates: list[Gate], *, fail_mode: str = "fail_closed") -> None:
        self._gates = list(gates)
        self._fail_mode = str(fail_mode or "fail_closed").strip() or "fail_closed"

    def evaluate(
        self,
        *,
        symbol: str,
        feature_context: FeatureContext,
        readiness: DataReadinessReport,
        runtime: GateRuntimeContext | None = None,
    ) -> GateEvaluationReport:
        decisions = []
        for gate in self._gates:
            try:
                decision = self._evaluate_gate_compat(
                    gate=gate,
                    symbol=symbol,
                    feature_context=feature_context,
                    readiness=readiness,
                    runtime=runtime,
                )
            except Exception as exc:
                if self._fail_mode == "fail_open":
                    decision = gate_failure_decision(gate_id=getattr(gate, "gate_id", "unknown"), reason=str(exc), allowed=True)
                else:
                    decision = gate_failure_decision(gate_id=getattr(gate, "gate_id", "unknown"), reason=str(exc), allowed=False)
            decisions.append(decision)
            if not decision.allowed:
                return GateEvaluationReport(
                    allowed=False,
                    decisions=tuple(decisions),
                    blocked_by=decision.gate_id,
                    fail_mode=self._fail_mode,
                )
        return GateEvaluationReport(allowed=True, decisions=tuple(decisions), blocked_by=None, fail_mode=self._fail_mode)

    @staticmethod
    def _evaluate_gate_compat(
        *,
        gate: Gate,
        symbol: str,
        feature_context: FeatureContext,
        readiness: DataReadinessReport,
        runtime: GateRuntimeContext | None,
    ) -> Any:
        """
        Compatibility adapter:
        - New gates can accept `runtime=...`
        - Legacy gates keep old signature without runtime
        """
        evaluate_fn = getattr(gate, "evaluate")
        if GatePipeline._supports_runtime_kw(evaluate_fn):
            return gate.evaluate(
                symbol=symbol,
                feature_context=feature_context,
                readiness=readiness,
                runtime=runtime,
            )
        return gate.evaluate(symbol=symbol, feature_context=feature_context, readiness=readiness)

    @staticmethod
    def _supports_runtime_kw(evaluate_fn: Any) -> bool:
        try:
            sig = inspect.signature(evaluate_fn)
        except (TypeError, ValueError):
            return False
        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == "runtime":
                return True
        return False


def gate_failure_decision(*, gate_id: str, reason: str, allowed: bool):
    from strategies.stocks_kernel.types import GateDecision

    return GateDecision(
        gate_id=str(gate_id or "unknown"),
        allowed=bool(allowed),
        reason="gate_failed_open" if allowed else "gate_failed_closed",
        trader_reason_code="gate_failure",
        payload={"error": str(reason or "")},
    )
