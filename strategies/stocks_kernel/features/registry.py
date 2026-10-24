from __future__ import annotations

from dataclasses import replace
from typing import Any

from strategies.stocks_kernel.types import DataContextBundle, FeatureContext, FeatureResult, HistoryBundle, MarketSnapshot

from .base import FeaturePlugin
from .builtins import (
    calc_atr_eff,
    calc_distance_from_vwap_atr,
    calc_pos_day,
    calc_v3m,
    calc_vol_surge,
    calc_vwap,
)


class FeatureRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, FeaturePlugin] = {}

    def register(self, plugin: FeaturePlugin) -> None:
        feature_id = str(plugin.feature_id or "").strip()
        if not feature_id:
            raise ValueError("feature_id is required")
        self._plugins[feature_id] = plugin

    def get(self, feature_id: str) -> FeaturePlugin | None:
        return self._plugins.get(str(feature_id or "").strip())

    def known_feature_ids(self) -> list[str]:
        return sorted(self._plugins.keys())

    def build_context(
        self,
        *,
        symbol: str,
        snapshot: MarketSnapshot,
        history: HistoryBundle,
        feature_ids: list[str],
        feature_params: dict[str, Any] | None = None,
        scope: str = "signal",
    ) -> FeatureContext:
        values: dict[str, object] = {}
        readiness: dict[str, str] = {}
        cache: dict[str, FeatureResult] = {}
        params_map = dict(feature_params or {})

        for feature_id in feature_ids:
            result = self._resolve_feature(
                feature_id=str(feature_id).strip(),
                snapshot=snapshot,
                history=history,
                params_map=params_map,
                cache=cache,
                stack=(),
            )
            values[str(feature_id).strip()] = result.value
            readiness[str(feature_id).strip()] = result.reason
        return FeatureContext(
            symbol=str(symbol).strip().upper(),
            snapshot=snapshot,
            history=history,
            values=values,
            readiness=readiness,
            scope=str(scope or "signal").strip().lower() or "signal",
        )

    def _resolve_feature(
        self,
        *,
        feature_id: str,
        snapshot: MarketSnapshot,
        history: HistoryBundle,
        params_map: dict[str, Any],
        cache: dict[str, FeatureResult],
        stack: tuple[str, ...],
    ) -> FeatureResult:
        if feature_id in cache:
            return cache[feature_id]
        if feature_id in stack:
            result = FeatureResult(
                value=None,
                ready=False,
                reason="feature_dependency_cycle",
                payload={"feature_id": feature_id, "stack": list(stack)},
            )
            cache[feature_id] = result
            return result
        plugin = self._plugins.get(feature_id)
        if plugin is None:
            result = FeatureResult(value=None, ready=False, reason="feature_unregistered")
            cache[feature_id] = result
            return result

        dependency_values: dict[str, Any] = {}
        dependency_readiness: dict[str, str] = {}
        for dependency in plugin.dependencies:
            dep_result = self._resolve_feature(
                feature_id=dependency,
                snapshot=snapshot,
                history=history,
                params_map=params_map,
                cache=cache,
                stack=stack + (feature_id,),
            )
            dependency_values[dependency] = dep_result.value
            dependency_readiness[dependency] = dep_result.reason

        merged_params = dict(plugin.default_params)
        raw_override = params_map.get(feature_id)
        if isinstance(raw_override, dict):
            merged_params.update(raw_override)
        snapshot_with_dependencies = replace(
            snapshot,
            extras={
                **dict(snapshot.extras),
                "_feature_dependency_values": dependency_values,
                "_feature_dependency_readiness": dependency_readiness,
            },
        )
        result = plugin.calculator(snapshot_with_dependencies, history, merged_params)
        cache[feature_id] = result
        return result

    def build_scoped_feature_overlay(
        self,
        *,
        data_context_bundle: DataContextBundle,
        scope_feature_ids: dict[str, list[str]],
        feature_params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        values: dict[str, Any] = {}
        readiness: dict[str, str] = {}
        params_map = dict(feature_params or {})
        for scope, feature_ids in dict(scope_feature_ids or {}).items():
            scope_v = str(scope or "").strip().lower()
            ids = [str(fid).strip() for fid in list(feature_ids or []) if str(fid).strip()]
            if not ids:
                continue
            contexts = dict(data_context_bundle.scopes().get(scope_v, {}) or {})
            if not contexts:
                continue
            valid_contexts: list[tuple[str, Any, Any]] = []
            for symbol, symbol_ctx in contexts.items():
                snapshot = getattr(symbol_ctx, "snapshot", None)
                history = getattr(symbol_ctx, "history", None)
                if snapshot is None or history is None:
                    continue
                valid_contexts.append((symbol, snapshot, history))
            aliases_enabled = len(valid_contexts) == 1
            for symbol, snapshot, history in valid_contexts:
                scoped_ctx = self.build_context(
                    symbol=symbol,
                    snapshot=snapshot,
                    history=history,
                    feature_ids=ids,
                    feature_params=params_map,
                    scope=scope_v,
                )
                for fid in ids:
                    v_key = f"{scope_v}.{symbol}.{fid}"
                    values[v_key] = scoped_ctx.values.get(fid)
                    readiness[v_key] = scoped_ctx.readiness.get(fid, "feature_missing")
                    if aliases_enabled:
                        a_key = f"{scope_v}.{fid}"
                        values[a_key] = scoped_ctx.values.get(fid)
                        readiness[a_key] = scoped_ctx.readiness.get(fid, "feature_missing")
        return values, readiness


def default_feature_registry() -> FeatureRegistry:
    registry = FeatureRegistry()
    registry.register(FeaturePlugin(feature_id="vwap", calculator=calc_vwap))
    registry.register(FeaturePlugin(feature_id="atr_eff", calculator=calc_atr_eff, default_params={"window": 14}))
    registry.register(FeaturePlugin(feature_id="v3m", calculator=calc_v3m, default_params={"bars": 3}))
    registry.register(FeaturePlugin(feature_id="pos_day", calculator=calc_pos_day))
    registry.register(FeaturePlugin(feature_id="vol_surge", calculator=calc_vol_surge, default_params={"window": 5}))
    registry.register(
        FeaturePlugin(
            feature_id="distance_from_vwap_atr",
            calculator=calc_distance_from_vwap_atr,
            dependencies=("vwap", "atr_eff"),
            default_params={"window": 14},
        )
    )
    return registry
