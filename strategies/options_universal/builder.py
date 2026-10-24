from __future__ import annotations

import importlib
from datetime import datetime, time
from typing import Any

from services.production.strategy_manifest_gate_service import build_strategy_manifest_gate
from strategies.options_universal.plugin import OptionStrategyManifest, OptionStrategyPlugin


def load_option_plugin(plugin_cls: str, *, plugin_params: dict[str, Any] | None = None) -> OptionStrategyPlugin:
    module_name, _, attr_name = str(plugin_cls or "").strip().partition(":")
    if not module_name or not attr_name:
        raise ValueError("plugin class must be MODULE:CLASS")
    module = importlib.import_module(module_name)
    target: Any = module
    for part in attr_name.split("."):
        target = getattr(target, part)
    plugin = target(**dict(plugin_params or {}))
    if not isinstance(plugin, OptionStrategyPlugin):
        raise TypeError("plugin class must instantiate OptionStrategyPlugin")
    return plugin


def build_option_strategy_instance_config(
    *,
    plugin: OptionStrategyPlugin,
    plugin_cls: str,
    strategy_id: str,
    enabled: bool = False,
    capital_limit_usd: float = 1000.0,
    cadence_s: int = 1,
    ttl_ms: int = 2000,
    plugin_params: dict[str, Any] | None = None,
    extra_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = plugin.production_manifest()
    if not isinstance(manifest, OptionStrategyManifest):
        raise ValueError("option plugin must provide OptionStrategyManifest")
    underlying = str(manifest.underlying or "").strip().upper()
    extras: dict[str, Any] = {
        "plugin_cls": str(plugin_cls),
        "plugin_component": str(plugin.component_name or plugin.__class__.__name__),
        "plugin_params": dict(plugin_params or {}),
        "underlying": underlying,
        "risk_symbol": f"OPT:{underlying}:{manifest.expiry_policy}",
        "definition_id": str(manifest.strategy_key or "").strip().upper(),
        "data_contract": manifest.data_contract(),
        "production_manifest": manifest.production_manifest(),
        "option_manifest": {
            "strategy_key": manifest.strategy_key,
            "underlying": underlying,
            "trigger_source_ids": list(manifest.trigger_source_ids),
            "expiry_policy": manifest.expiry_policy,
            "allowed_rights": list(manifest.allowed_rights),
            "max_spread_pct": float(manifest.max_spread_pct),
            "quote_ttl_sec": float(manifest.quote_ttl_sec),
            "option_chain_source": manifest.option_chain_source,
            "contracts": int(manifest.contracts),
            "max_premium_usd": float(manifest.max_premium_usd),
            "max_daily_loss_usd": float(manifest.max_daily_loss_usd),
        },
        "contracts": int(manifest.contracts),
        "max_premium_usd": float(manifest.max_premium_usd),
        "max_daily_loss_usd": float(manifest.max_daily_loss_usd),
        "max_spread_pct": float(manifest.max_spread_pct),
        "quote_ttl_sec": float(manifest.quote_ttl_sec),
        "option_chain_source": str(manifest.option_chain_source),
        "allowed_rights": list(manifest.allowed_rights),
        "trigger_source_ids": list(manifest.trigger_source_ids),
        "ab_time_start": str(manifest.entry_start),
        "ab_time_end": str(manifest.entry_end),
        "no_new_entry_after": str(manifest.no_new_entry_after),
        "force_flatten_time": str(manifest.force_flatten_time),
    }
    extras.update(dict(extra_extras or {}))
    return {
        "id": str(strategy_id or "").strip(),
        "enabled": bool(enabled),
        "module": "strategies.options_universal.base_strategy",
        "class": "UniversalOptionLiveStrategy",
        "capital_limit_usd": float(capital_limit_usd),
        "params": {
            "cadence_s": int(cadence_s),
            "ttl_ms": int(ttl_ms),
            "extras": extras,
        },
    }


def validate_option_strategy_config(strategy_config: dict[str, Any]) -> list[str]:
    strategy_id = str(strategy_config.get("id") or "").strip() or "UNKNOWN_STRATEGY"
    params = strategy_config.get("params") if isinstance(strategy_config.get("params"), dict) else {}
    extras = params.get("extras") if isinstance(params.get("extras"), dict) else {}
    manifest = extras.get("option_manifest") if isinstance(extras.get("option_manifest"), dict) else {}
    blockers: list[str] = []
    underlying = str(manifest.get("underlying") or extras.get("underlying") or "").strip().upper()
    if not underlying:
        blockers.append("underlying_missing")
    triggers = [str(item or "").strip() for item in manifest.get("trigger_source_ids") or extras.get("trigger_source_ids") or []]
    if not [item for item in triggers if item]:
        blockers.append("trigger_source_ids_missing")
    rights = {str(item or "").strip().upper() for item in manifest.get("allowed_rights") or extras.get("allowed_rights") or []}
    if not rights or not rights <= {"CALL", "PUT"}:
        blockers.append("allowed_rights_invalid")
    if _float_value(manifest.get("max_spread_pct") or extras.get("max_spread_pct")) <= 0.0:
        blockers.append("max_spread_pct_missing_or_invalid")
    if _float_value(manifest.get("quote_ttl_sec") or extras.get("quote_ttl_sec")) <= 0.0:
        blockers.append("quote_ttl_sec_missing_or_invalid")
    if _int_value(manifest.get("contracts") or extras.get("contracts")) <= 0:
        blockers.append("contracts_missing_or_invalid")
    cutoff = _parse_hms(extras.get("no_new_entry_after"))
    flatten = _parse_hms(extras.get("force_flatten_time"))
    if cutoff is None:
        blockers.append("entry_cutoff_missing_or_invalid")
    if flatten is None:
        blockers.append("force_flatten_time_missing_or_invalid")
    if cutoff is not None and flatten is not None and cutoff > flatten:
        blockers.append("entry_cutoff_after_force_flatten")
    gate = build_strategy_manifest_gate(config={"strategies": [strategy_config]}, generated_at=datetime.now())
    blockers.extend(str(item).split(":", 1)[1] if ":" in str(item) else str(item) for item in gate.get("blockers") or [])
    return [f"{strategy_id}:{blocker}" for blocker in blockers]


def _parse_hms(value: Any) -> time | None:
    try:
        parts = [int(part) for part in str(value or "").strip().strip("'\"").split(":")]
        if len(parts) == 2:
            return time(parts[0], parts[1])
        if len(parts) == 3:
            return time(parts[0], parts[1], parts[2])
    except Exception:
        return None
    return None


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0
