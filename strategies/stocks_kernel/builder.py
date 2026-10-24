from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .plugin import BaseStockPlugin, StockStrategyManifest
from .templates import default_template_registry


@dataclass(frozen=True)
class StrategyLegBinding:
    signal_symbol: str
    long_exec_symbol: str
    short_exec_symbol: str
    confirm_symbol: str | None = None
    volatility_symbol: str | None = None


@dataclass(frozen=True)
class StrategyInstanceSpec:
    strategy_id: str
    strategy_template: str
    signal_symbol: str
    long_exec_symbol: str
    short_exec_symbol: str
    enabled: bool = False
    capital_limit_usd: float = 100000.0
    cadence_s: int = 5
    ttl_ms: int = 2000
    dry_run: bool = True
    order_namespace: str = "stkv1"
    audit_root_dir: str = "runtime/audit"
    feature_bar_sec: int = 5
    gate_fail_mode: str = "fail_closed"
    startup_preflight_enabled: bool = True
    confirm_symbol: str | None = None
    volatility_symbol: str | None = None
    feature_params: dict[str, Any] = field(default_factory=dict)
    gate_overrides: dict[str, Any] = field(default_factory=dict)
    policy_params: dict[str, Any] = field(default_factory=dict)
    execution_profile: dict[str, Any] = field(default_factory=dict)
    extra_extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyInstanceBlueprint:
    strategy_template: str
    universe: list[dict[str, Any]]
    dry_run: bool = True
    order_namespace: str = "stkv1"
    audit_root_dir: str = "runtime/audit"
    feature_bar_sec: int = 5
    gate_fail_mode: str = "fail_closed"
    startup_preflight_enabled: bool = True
    feature_params: dict[str, Any] = field(default_factory=dict)
    gate_overrides: dict[str, Any] = field(default_factory=dict)
    policy_params: dict[str, Any] = field(default_factory=dict)
    execution_profile: dict[str, Any] = field(default_factory=dict)
    extra_extras: dict[str, Any] = field(default_factory=dict)


def build_strategy_blueprint(
    *,
    strategy_template: str,
    signal_symbol: str,
    long_exec_symbol: str,
    short_exec_symbol: str,
    confirm_symbol: str | None = None,
    volatility_symbol: str | None = None,
    dry_run: bool = True,
    order_namespace: str = "stkv1",
    audit_root_dir: str = "runtime/audit",
    feature_bar_sec: int = 5,
    gate_fail_mode: str = "fail_closed",
    startup_preflight_enabled: bool = True,
    feature_params: dict[str, Any] | None = None,
    gate_overrides: dict[str, Any] | None = None,
    policy_params: dict[str, Any] | None = None,
    execution_profile: dict[str, Any] | None = None,
    extra_extras: dict[str, Any] | None = None,
) -> StrategyInstanceBlueprint:
    leg = StrategyLegBinding(
        signal_symbol=str(signal_symbol or "").strip().upper(),
        long_exec_symbol=str(long_exec_symbol or "").strip().upper(),
        short_exec_symbol=str(short_exec_symbol or "").strip().upper(),
        confirm_symbol=str(confirm_symbol or "").strip().upper() or None,
        volatility_symbol=str(volatility_symbol or "").strip().upper() or None,
    )
    return StrategyInstanceBlueprint(
        strategy_template=str(strategy_template or "").strip(),
        universe=[_binding_to_dict(leg)],
        dry_run=bool(dry_run),
        order_namespace=str(order_namespace or "").strip() or "stkv1",
        audit_root_dir=str(audit_root_dir or "").strip() or "runtime/audit",
        feature_bar_sec=int(feature_bar_sec),
        gate_fail_mode=str(gate_fail_mode or "").strip() or "fail_closed",
        startup_preflight_enabled=bool(startup_preflight_enabled),
        feature_params=dict(feature_params or {}),
        gate_overrides=dict(gate_overrides or {}),
        policy_params=dict(policy_params or {}),
        execution_profile=dict(execution_profile or {}),
        extra_extras=dict(extra_extras or {}),
    )


def build_plugin_strategy_blueprint(
    *,
    plugin: BaseStockPlugin,
    strategy_id: str,
    strategy_template: str,
    signal_symbol: str,
    long_exec_symbol: str,
    short_exec_symbol: str,
    confirm_symbol: str | None = None,
    volatility_symbol: str | None = None,
    dry_run: bool = True,
    order_namespace: str = "stkv1",
    audit_root_dir: str = "runtime/audit",
    feature_bar_sec: int = 5,
    gate_fail_mode: str = "fail_closed",
    startup_preflight_enabled: bool = True,
    feature_params: dict[str, Any] | None = None,
    gate_overrides: dict[str, Any] | None = None,
    policy_params: dict[str, Any] | None = None,
    execution_profile: dict[str, Any] | None = None,
    extra_extras: dict[str, Any] | None = None,
) -> StrategyInstanceBlueprint:
    if not isinstance(plugin, BaseStockPlugin):
        raise TypeError("plugin must be a BaseStockPlugin")
    plugin_payload = plugin.build_policy_payload(strategy_id=strategy_id, extras=extra_extras)
    merged_extras = dict(extra_extras or {})
    merged_extras.setdefault("plugin_component", str(plugin.component_name or plugin.__class__.__name__))
    merged_extras.setdefault("plugin_policy_id", str(plugin.policy_id()))
    merged_extras.setdefault("plugin_policy_payload", plugin_payload)
    plugin_cls = f"{plugin.__class__.__module__}:{plugin.__class__.__qualname__}"
    if "<locals>" not in plugin_cls:
        merged_extras.setdefault("plugin_cls", plugin_cls)
    return build_strategy_blueprint(
        strategy_template=strategy_template,
        signal_symbol=signal_symbol,
        long_exec_symbol=long_exec_symbol,
        short_exec_symbol=short_exec_symbol,
        confirm_symbol=confirm_symbol,
        volatility_symbol=volatility_symbol,
        dry_run=dry_run,
        order_namespace=order_namespace,
        audit_root_dir=audit_root_dir,
        feature_bar_sec=feature_bar_sec,
        gate_fail_mode=gate_fail_mode,
        startup_preflight_enabled=startup_preflight_enabled,
        feature_params=feature_params,
        gate_overrides=gate_overrides,
        policy_params=policy_params,
        execution_profile=execution_profile,
        extra_extras=merged_extras,
    )


def build_manifest_strategy_instance_config(
    *,
    plugin: BaseStockPlugin,
    strategy_id: str,
    enabled: bool = False,
    capital_limit_usd: float = 250.0,
    cadence_s: int = 5,
    ttl_ms: int = 30000,
    dry_run: bool = False,
    qty: float = 1.0,
    trade_budget_usd: float | None = None,
    trade_interval_sec: int = 60,
    max_cycles_per_day: int = 240,
    plugin_params: dict[str, Any] | None = None,
    policy_params: dict[str, Any] | None = None,
    extra_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(plugin, BaseStockPlugin):
        raise TypeError("plugin must be a BaseStockPlugin")
    manifest = plugin.production_manifest()
    if not isinstance(manifest, StockStrategyManifest):
        raise ValueError("plugin must provide StockStrategyManifest")
    symbols = [str(item or "").strip().upper() for item in manifest.symbols if str(item or "").strip()]
    if not symbols:
        raise ValueError("manifest.symbols is required")
    symbol = symbols[0]
    plugin_cls = f"{plugin.__class__.__module__}:{plugin.__class__.__qualname__}"
    if "<locals>" in plugin_cls:
        raise ValueError("plugin class must be importable")
    merged_policy_params = {
        "symbol": symbol,
        "qty": float(qty),
        "trade_budget_usd": float(trade_budget_usd if trade_budget_usd is not None else capital_limit_usd),
        "trade_interval_sec": int(trade_interval_sec),
        "max_cycles_per_day": int(max_cycles_per_day),
        **dict(policy_params or {}),
    }
    extras: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "order_namespace": str(manifest.order_namespace or "stkv1"),
        "policy_id": str(manifest.policy_id or ""),
        "data_profile": str(manifest.data_profile or ""),
        "audit_root_dir": "runtime/audit",
        "feature_bar_sec": 5,
        "feature_set": [],
        "gate_pipeline": list(manifest.gate_pipeline),
        "enable_dynamic_policy_registry": bool(manifest.policy_registry_specs),
        "enable_policy_factory_runtime": bool(manifest.policy_registry_specs),
        "policy_registry_specs": [dict(item) for item in manifest.policy_registry_specs],
        "plugin_cls": plugin_cls,
        "plugin_params": dict(plugin_params or {}),
        "signal_symbol": symbol,
        "long_exec_symbol": symbol,
        "short_exec_symbol": symbol,
        "universe": [
            {
                "signal_symbol": symbol,
                "long_exec_symbol": symbol,
                "short_exec_symbol": symbol,
                "execution_lanes": [
                    {
                        "lane_id": f"{symbol.lower()}_long_exec",
                        "symbol": symbol,
                        "side": "LONG",
                        "role": "execution",
                        "ownership_scope": "strategy",
                        "flatten_authority": "strategy",
                        "protection_authority": "strategy",
                    }
                ],
            }
        ],
        "no_new_after_et": "23:30",
        "flatten_before_close_et": "23:59",
        "broker_truth_sync_interval_sec_offsession": 30,
        "startup_preflight_enabled": True,
        "require_exec_symbol_snapshot": True,
        "data_contract": manifest.data_contract(),
        "production_manifest": manifest.production_manifest(),
        "gate_overrides": manifest.default_gate_overrides(),
        "policy_params": merged_policy_params,
        "execution_profile": dict(manifest.execution_profile or {}),
        "plugin_manifest": {
            "strategy_key": manifest.strategy_key,
            "symbols": symbols,
            "template_id": manifest.template_id,
            "source": plugin_cls,
        },
    }
    extras.update(dict(extra_extras or {}))
    return {
        "id": str(strategy_id or "").strip(),
        "enabled": bool(enabled),
        "registry_name": "stocks_kernel",
        "module": "strategies.stocks_kernel_strategy",
        "class": "StocksKernelStrategy",
        "capital_limit_usd": float(capital_limit_usd),
        "params": {
            "cadence_s": int(cadence_s),
            "ttl_ms": int(ttl_ms),
            "extras": extras,
        },
    }


def template_catalog() -> list[dict[str, object]]:
    return default_template_registry().catalog()


def build_strategy_instance_config(spec: StrategyInstanceSpec) -> dict[str, Any]:
    blueprint = build_strategy_blueprint(
        strategy_template=spec.strategy_template,
        signal_symbol=spec.signal_symbol,
        long_exec_symbol=spec.long_exec_symbol,
        short_exec_symbol=spec.short_exec_symbol,
        confirm_symbol=spec.confirm_symbol,
        volatility_symbol=spec.volatility_symbol,
        dry_run=spec.dry_run,
        order_namespace=spec.order_namespace,
        audit_root_dir=spec.audit_root_dir,
        feature_bar_sec=spec.feature_bar_sec,
        gate_fail_mode=spec.gate_fail_mode,
        startup_preflight_enabled=spec.startup_preflight_enabled,
        feature_params=spec.feature_params,
        gate_overrides=spec.gate_overrides,
        policy_params=spec.policy_params,
        execution_profile=spec.execution_profile,
        extra_extras=spec.extra_extras,
    )
    return {
        "id": str(spec.strategy_id).strip(),
        "enabled": bool(spec.enabled),
        "module": "strategies.stocks_kernel_strategy",
        "class": "StocksKernelStrategy",
        "capital_limit_usd": float(spec.capital_limit_usd),
        "params": {
            "cadence_s": int(spec.cadence_s),
            "ttl_ms": int(spec.ttl_ms),
            "extras": build_strategy_extras(blueprint),
        },
    }


def validate_blueprint(blueprint: StrategyInstanceBlueprint) -> None:
    template_registry = default_template_registry()
    template_registry.get(blueprint.strategy_template)
    if not blueprint.universe:
        raise ValueError("universe is required")
    seen_signal_symbols: set[str] = set()
    for item in blueprint.universe:
        binding = _normalize_binding(item)
        if binding.signal_symbol in seen_signal_symbols:
            raise ValueError(f"duplicate signal symbol in blueprint: {binding.signal_symbol}")
        seen_signal_symbols.add(binding.signal_symbol)
        if binding.long_exec_symbol == binding.short_exec_symbol:
            raise ValueError(f"long_exec_symbol and short_exec_symbol must differ for {binding.signal_symbol}")
        if not binding.signal_symbol or not binding.long_exec_symbol or not binding.short_exec_symbol:
            raise ValueError("signal_symbol/long_exec_symbol/short_exec_symbol are required")


def build_strategy_extras(blueprint: StrategyInstanceBlueprint) -> dict[str, Any]:
    validate_blueprint(blueprint)
    template = default_template_registry().get(blueprint.strategy_template)
    normalized_universe = [_binding_to_dict(_normalize_binding(item)) for item in blueprint.universe]
    extras: dict[str, Any] = {
        "strategy_template": blueprint.strategy_template,
        "order_namespace": blueprint.order_namespace,
        "dry_run": blueprint.dry_run,
        "audit_root_dir": blueprint.audit_root_dir,
        "feature_bar_sec": blueprint.feature_bar_sec,
        "gate_fail_mode": blueprint.gate_fail_mode,
        "startup_preflight_enabled": blueprint.startup_preflight_enabled,
        "data_profile": template.data_profile,
        "policy_id": template.policy_id,
        "feature_set": list(template.feature_set),
        "gate_pipeline": list(template.gate_pipeline),
        "feature_params": {
            **dict(template.feature_params),
            **dict(blueprint.feature_params),
        },
        "gate_overrides": {
            **dict(template.gate_overrides),
            **dict(blueprint.gate_overrides),
        },
        "policy_params": {
            **dict(template.policy_params),
            **dict(blueprint.policy_params),
        },
        "execution_profile": {
            **dict(template.execution_profile),
            **dict(blueprint.execution_profile),
        },
        "universe": normalized_universe,
    }
    extras.update(dict(blueprint.extra_extras))
    return extras


def _normalize_binding(item: dict[str, Any]) -> StrategyLegBinding:
    if not isinstance(item, dict):
        raise ValueError("universe items must be dict")
    return StrategyLegBinding(
        signal_symbol=str(item.get("signal_symbol") or "").strip().upper(),
        long_exec_symbol=str(item.get("long_exec_symbol") or "").strip().upper(),
        short_exec_symbol=str(item.get("short_exec_symbol") or "").strip().upper(),
        confirm_symbol=str(item.get("confirm_symbol") or "").strip().upper() or None,
        volatility_symbol=str(item.get("volatility_symbol") or "").strip().upper() or None,
    )


def _binding_to_dict(binding: StrategyLegBinding) -> dict[str, Any]:
    out: dict[str, Any] = {
        "signal_symbol": binding.signal_symbol,
        "long_exec_symbol": binding.long_exec_symbol,
        "short_exec_symbol": binding.short_exec_symbol,
    }
    if binding.confirm_symbol:
        out["confirm_symbol"] = binding.confirm_symbol
    if binding.volatility_symbol:
        out["volatility_symbol"] = binding.volatility_symbol
    return out
