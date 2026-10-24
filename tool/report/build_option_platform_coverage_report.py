from __future__ import annotations

import argparse
import json
import importlib
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.options_kernel.inventory import build_option_strategy_inventory


def _load_yaml(path: str | Path) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _load_plugin_from_row(strategy_row: Dict[str, Any]) -> Any:
    module_name = str(strategy_row.get("module") or "").strip()
    class_name = str(strategy_row.get("class") or "").strip()
    module = importlib.import_module(module_name)
    strategy_cls = getattr(module, class_name)
    plugin_cls = getattr(strategy_cls, "plugin_cls", None)
    if plugin_cls is None:
        raise ValueError(f"strategy_id={strategy_row.get('id')} missing plugin_cls")
    return plugin_cls()


def build_option_platform_coverage_report(config_path: str | Path) -> Dict[str, Any]:
    config_payload = _load_yaml(config_path)
    inventory = build_option_strategy_inventory(config_payload, retired_ids=())
    strategies = [row for row in list(config_payload.get("strategies") or []) if isinstance(row, dict)]
    rows: List[Dict[str, Any]] = []
    archetypes = set()
    for inv_row in inventory.get("strategies") or []:
        strategy_id = str(inv_row.get("strategy_id") or "")
        strategy_row = next((row for row in strategies if str(row.get("id") or "") == strategy_id), None)
        if strategy_row is None:
            continue
        try:
            plugin = _load_plugin_from_row(strategy_row)
            extras = (((strategy_row.get("params") or {}).get("extras")) or {})
            resolved = dict(plugin.apply_default_extras(dict(extras or {})))
            identity = {
                "definition_id": str(resolved.get("definition_id") or ""),
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
            }
            archetype = str(identity["strategy_archetype"] or "")
            if archetype:
                archetypes.add(archetype)
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "enabled": bool(strategy_row.get("enabled", False)),
                    "definition_id": identity["definition_id"],
                    "strategy_family": identity["strategy_family"],
                    "strategy_archetype": archetype,
                    "underlying": str(resolved.get("underlying") or ""),
                    "module_identity": identity,
                    "report_status": "ok",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "enabled": bool(strategy_row.get("enabled", False)),
                    "definition_id": str(((((strategy_row.get("params") or {}).get("extras")) or {}).get("definition_id")) or ""),
                    "strategy_family": "",
                    "strategy_archetype": "",
                    "underlying": str((((strategy_row.get("params") or {}).get("extras")) or {}).get("underlying") or ""),
                    "module_identity": {},
                    "report_status": "error",
                    "error": str(exc),
                }
            )
    rows.sort(key=lambda row: row["strategy_id"])
    return {
        "summary": {
            "strategy_sample_count": len(rows),
            "distinct_archetype_count": len({row["strategy_archetype"] for row in rows if row["strategy_archetype"]}),
            "coverage_goal_met": len({row["strategy_archetype"] for row in rows if row["strategy_archetype"]}) >= 3,
            "error_count": len([row for row in rows if row.get("report_status") != "ok"]),
        },
        "strategy_samples": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build option platform coverage report")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = build_option_platform_coverage_report(args.config)
    payload = json.dumps(report, indent=2, sort_keys=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
