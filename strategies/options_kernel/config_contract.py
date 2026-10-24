from __future__ import annotations

import importlib
from typing import Any, Dict, List, Mapping


REQUIRED_TOP_LEVEL_KEYS = ("id", "module", "class", "params")
REQUIRED_EXTRA_KEYS = (
    "definition_id",
    "underlying",
    "risk_symbol",
    "kernel_mode",
    "strict_scope_validation",
    "shared_cap_group_id",
    "shared_cap_group_strategy_ids",
)
PLUGIN_KERNEL_MODES = {"plugin", "pure_plugin", "hybrid"}


def _instantiate_plugin(strategy_row: Mapping[str, Any]) -> Any:
    module_name = str(strategy_row.get("module") or "").strip()
    class_name = str(strategy_row.get("class") or "").strip()
    module = importlib.import_module(module_name)
    strategy_cls = getattr(module, class_name)
    plugin_cls = getattr(strategy_cls, "plugin_cls", None)
    if plugin_cls is None:
        raise ValueError(f"strategy_id={strategy_row.get('id')} missing plugin_cls")
    return plugin_cls()


def validate_option_strategy_config_row(strategy_row: Mapping[str, Any]) -> Dict[str, Any]:
    row = dict(strategy_row or {})
    strategy_id = str(row.get("id") or "").strip()
    issues: List[str] = []
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in row:
            issues.append(f"missing_top_level_key:{key}")
    params = dict(row.get("params") or {})
    extras = dict(params.get("extras") or {})
    for key in REQUIRED_EXTRA_KEYS:
        value = extras.get(key)
        if key == "shared_cap_group_strategy_ids":
            if not isinstance(value, list) or not value:
                issues.append(f"missing_or_invalid_extra:{key}")
            continue
        if value in (None, "", ()):
            issues.append(f"missing_or_invalid_extra:{key}")

    try:
        plugin = _instantiate_plugin(row)
        resolved = dict(plugin.apply_default_extras(dict(extras)))
    except Exception as exc:
        issues.append(f"plugin_resolution_failed:{exc}")
        return {
            "strategy_id": strategy_id,
            "issues": issues,
            "pass": False,
            "resolved_identity": {},
        }

    if not bool(getattr(plugin, "assembly_adapter_mode", False)):
        issues.append("plugin_not_marked_as_assembly_adapter")
    if str(resolved.get("kernel_mode") or "").strip().lower() not in PLUGIN_KERNEL_MODES:
        issues.append("kernel_mode_not_plugin_family")
    if not bool(resolved.get("strict_scope_validation", False)):
        issues.append("strict_scope_validation_not_enabled")

    definition_id = str(resolved.get("definition_id") or "").strip()
    if not definition_id:
        issues.append("definition_id_missing_after_resolution")
    if extras.get("definition_id") not in (None, "", definition_id):
        issues.append("definition_id_drift_between_config_and_definition")

    underlying = str(resolved.get("underlying") or "").strip().upper()
    if str(extras.get("underlying") or "").strip().upper() != underlying:
        issues.append("underlying_drift_between_config_and_definition")
    risk_symbol = str(resolved.get("risk_symbol") or "").strip()
    if str(extras.get("risk_symbol") or "").strip() != risk_symbol:
        issues.append("risk_symbol_drift_between_config_and_definition")

    group_ids = list(extras.get("shared_cap_group_strategy_ids") or [])
    if strategy_id and strategy_id not in group_ids:
        issues.append("shared_cap_group_strategy_ids_missing_self")
    if str(extras.get("shared_cap_group_id") or "").strip() == "":
        issues.append("shared_cap_group_id_empty")

    resolved_identity = {
        "definition_id": definition_id,
        "strategy_family": str(resolved.get("strategy_family") or ""),
        "strategy_archetype": str(resolved.get("strategy_archetype") or ""),
        "signal_module_id": str(resolved.get("signal_module_id") or ""),
        "direction_mapping_policy": str(resolved.get("direction_mapping_policy") or ""),
        "contract_policy_id": str(resolved.get("contract_policy_id") or ""),
        "execution_policy_id": str(resolved.get("execution_policy_id") or ""),
        "position_policy_id": str(resolved.get("position_policy_id") or ""),
        "risk_policy_id": str(resolved.get("risk_policy_id") or ""),
        "profit_management_policy_id": str(resolved.get("profit_management_policy_id") or ""),
        "flatten_policy_id": str(resolved.get("flatten_policy_id") or ""),
        "ownership_scope": str(resolved.get("ownership_scope") or ""),
        "kernel_mode": str(resolved.get("kernel_mode") or ""),
    }
    for key, value in resolved_identity.items():
        if key == "kernel_mode":
            continue
        if not str(value or "").strip():
            issues.append(f"resolved_identity_missing:{key}")

    return {
        "strategy_id": strategy_id,
        "issues": issues,
        "pass": not issues,
        "resolved_identity": resolved_identity,
    }
