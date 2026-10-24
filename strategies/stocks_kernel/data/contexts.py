from __future__ import annotations

from typing import Any

from strategies.stocks_kernel.types import DataContextBundle, DataReadinessReport, FeatureContext, HistoryBundle, MarketSnapshot, SymbolDataContext


def build_data_context_bundle(
    *,
    signal_symbol: str,
    signal_snapshot: MarketSnapshot,
    signal_history: HistoryBundle,
    signal_readiness: DataReadinessReport,
    signal_feature_context: FeatureContext,
    exec_contexts: dict[str, dict[str, Any]] | None = None,
    confirm_contexts: dict[str, dict[str, Any]] | None = None,
    hedge_contexts: dict[str, dict[str, Any]] | None = None,
) -> DataContextBundle:
    def _build_map(scope: str, source: dict[str, dict[str, Any]] | None) -> dict[str, SymbolDataContext]:
        out: dict[str, SymbolDataContext] = {}
        for symbol, payload in dict(source or {}).items():
            symbol_u = str(symbol or "").strip().upper()
            if not symbol_u:
                continue
            out[symbol_u] = SymbolDataContext(
                scope=scope,
                symbol=symbol_u,
                snapshot=payload.get("snapshot"),
                history=payload.get("history"),
                readiness=payload.get("readiness"),
                feature_context=payload.get("feature_context"),
            )
        return out

    return DataContextBundle(
        signal=SymbolDataContext(
            scope="signal",
            symbol=str(signal_symbol or "").strip().upper(),
            snapshot=signal_snapshot,
            history=signal_history,
            readiness=signal_readiness,
            feature_context=signal_feature_context,
        ),
        execution=_build_map("execution", exec_contexts),
        confirm=_build_map("confirm", confirm_contexts),
        hedge=_build_map("hedge", hedge_contexts),
    )
