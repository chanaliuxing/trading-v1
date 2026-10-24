from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.options_kernel.inventory import build_option_strategy_inventory_from_config


REQUIRED_IDENTITY_KEYS = (
    "definition_id",
    "strategy_family",
    "signal_module_id",
    "direction_mapping_policy",
    "contract_policy_id",
    "execution_policy_id",
    "position_policy_id",
    "risk_policy_id",
    "profit_management_policy_id",
    "flatten_policy_id",
    "ownership_scope",
)


def _load_yaml(path: str | Path) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _load_strategy_row(config_payload: Dict[str, Any], strategy_id: str) -> Dict[str, Any]:
    for row in list(config_payload.get("strategies") or []):
        if isinstance(row, dict) and str(row.get("id") or "") == strategy_id:
            return row
    raise KeyError(f"strategy_id={strategy_id} missing from config")


def _instantiate_plugin(strategy_row: Dict[str, Any]) -> Any:
    module_name = str(strategy_row.get("module") or "").strip()
    class_name = str(strategy_row.get("class") or "").strip()
    module = importlib.import_module(module_name)
    strategy_cls = getattr(module, class_name)
    plugin_cls = getattr(strategy_cls, "plugin_cls", None)
    if plugin_cls is None:
        raise ValueError(f"strategy_id={strategy_row.get('id')} missing plugin_cls")
    return plugin_cls()


def _build_strategy_result(strategy_row: Dict[str, Any]) -> Dict[str, Any]:
    strategy_id = str(strategy_row.get("id") or "")
    plugin = _instantiate_plugin(strategy_row)
    extras = (((strategy_row.get("params") or {}).get("extras")) or {})
    resolved = dict(plugin.apply_default_extras(dict(extras or {})))
    missing_identity = [key for key in REQUIRED_IDENTITY_KEYS if not str(resolved.get(key) or "").strip()]
    issues: List[str] = []
    if not bool(getattr(plugin, "assembly_adapter_mode", False)):
        issues.append("plugin_not_marked_as_assembly_adapter")
    if missing_identity:
        issues.append(f"missing_identity_keys:{','.join(missing_identity)}")
    if str(resolved.get("kernel_mode") or "").strip().lower() not in {"plugin", "pure_plugin", "hybrid"}:
        issues.append("kernel_mode_not_plugin_family")
    if not str(resolved.get("definition_id") or "").strip():
        issues.append("definition_id_missing")

    return {
        "strategy_id": strategy_id,
        "plugin_component": str(getattr(plugin, "component_name", "")),
        "assembly_adapter_mode": bool(getattr(plugin, "assembly_adapter_mode", False)),
        "supports_pure_kernel": bool(getattr(plugin, "supports_pure_kernel", False)),
        "kernel_mode": str(resolved.get("kernel_mode") or ""),
        "definition_id": str(resolved.get("definition_id") or ""),
        "identity": {key: resolved.get(key) for key in REQUIRED_IDENTITY_KEYS},
        "issues": issues,
        "pass": not issues,
    }


def build_option_assembly_adapter_report(config_path: str | Path) -> Dict[str, Any]:
    config_payload = _load_yaml(config_path)
    inventory = build_option_strategy_inventory_from_config(config_path)
    strategy_results = []
    for strategy_id in inventory.get("active_strategy_ids") or []:
        row = _load_strategy_row(config_payload, strategy_id)
        strategy_results.append(_build_strategy_result(row))
    failing = [row["strategy_id"] for row in strategy_results if not row["pass"]]
    return {
        "summary": {
            "active_option_strategy_count": len(strategy_results),
            "assembly_adapter_pass_count": len(strategy_results) - len(failing),
            "assembly_adapter_fail_count": len(failing),
        },
        "failing_strategy_ids": failing,
        "strategies": strategy_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build option assembly-adapter compliance report")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    report = build_option_assembly_adapter_report(args.config)
    payload = json.dumps(report, indent=2, sort_keys=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
