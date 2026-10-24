from __future__ import annotations

from strategies.stocks_kernel.gates.base import Gate, GateRuntimeContext
from strategies.stocks_kernel.types import DataReadinessReport, FeatureContext, GateDecision


class MultiScopeReadinessGate(Gate):
    gate_id = "multi_scope_readiness"

    def __init__(
        self,
        *,
        required_scopes: list[str] | None = None,
        execution_required: bool = False,
        confirm_required: bool = False,
        hedge_required: bool = False,
        allow_missing_runtime_context: bool = True,
    ) -> None:
        requested = [str(scope or "").strip().lower() for scope in list(required_scopes or []) if str(scope or "").strip()]
        if execution_required and "execution" not in requested:
            requested.append("execution")
        if confirm_required and "confirm" not in requested:
            requested.append("confirm")
        if hedge_required and "hedge" not in requested:
            requested.append("hedge")
        self._required_scopes = tuple(scope for scope in requested if scope in {"execution", "confirm", "hedge"})
        self._allow_missing_runtime_context = bool(allow_missing_runtime_context)

    def evaluate(
        self,
        *,
        symbol: str,
        feature_context: FeatureContext,
        readiness: DataReadinessReport,
        runtime: GateRuntimeContext | None = None,
    ) -> GateDecision:
        _ = (symbol, readiness)
        if not self._required_scopes:
            return GateDecision(
                gate_id=self.gate_id,
                allowed=True,
                reason="multi_scope_not_required",
                trader_reason_code="multi_scope_not_required",
                payload={"required_scopes": []},
            )

        bundle = getattr(runtime, "data_context_bundle", None) if runtime is not None else None
        if bundle is None:
            # Backward-compat legacy injection path.
            bundle = feature_context.values.get("__data_context_bundle__")
        if bundle is None:
            return GateDecision(
                gate_id=self.gate_id,
                allowed=bool(self._allow_missing_runtime_context),
                reason="multi_scope_runtime_context_missing",
                trader_reason_code="multi_scope_runtime_context_missing",
                payload={
                    "required_scopes": list(self._required_scopes),
                    "allow_missing_runtime_context": bool(self._allow_missing_runtime_context),
                },
            )

        missing_scopes: list[str] = []
        not_ready_scopes: list[str] = []
        for scope in self._required_scopes:
            symbols = tuple(bundle.symbols_for_scope(scope))
            if not symbols:
                missing_scopes.append(scope)
                continue
            scope_ready = False
            for scope_symbol in symbols:
                context = bundle.context_for(scope=scope, symbol=scope_symbol)
                if bool(getattr(getattr(context, "readiness", None), "ready", False)):
                    scope_ready = True
                    break
            if not scope_ready:
                not_ready_scopes.append(scope)

        allowed = not missing_scopes and not not_ready_scopes
        reason = "ready" if allowed else ("multi_scope_missing" if missing_scopes else "multi_scope_not_ready")
        return GateDecision(
            gate_id=self.gate_id,
            allowed=allowed,
            reason=reason,
            trader_reason_code=reason,
            severity="warning" if allowed else "error",
            recoverable=not allowed,
            block_scope="lane",
            payload={
                "required_scopes": list(self._required_scopes),
                "missing_scopes": missing_scopes,
                "not_ready_scopes": not_ready_scopes,
            },
        )
