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

from strategies.options_kernel.inventory import build_option_strategy_inventory
from strategies.options_kernel.modules import definition_registry


def _load_yaml(path: str | Path) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _load_plugin(strategy_row: Dict[str, Any]) -> Any:
    module = importlib.import_module(str(strategy_row.get("module") or "").strip())
    strategy_cls = getattr(module, str(strategy_row.get("class") or "").strip())
    plugin_cls = getattr(strategy_cls, "plugin_cls", None)
    if plugin_cls is None:
        raise ValueError(f"strategy_id={strategy_row.get('id')} missing plugin_cls")
    return plugin_cls()


def build_option_strategy_rollout_evidence_report(config_path: str | Path) -> Dict[str, Any]:
    payload = _load_yaml(config_path)
    inventory = build_option_strategy_inventory(payload, retired_ids=())
    config_rows = {
        str(row.get("id") or ""): row
        for row in list(payload.get("strategies") or [])
        if isinstance(row, dict)
    }
    rows: List[Dict[str, Any]] = []
    for inv_row in list(inventory.get("strategies") or []):
        strategy_id = str(inv_row.get("strategy_id") or "")
        strategy_row = config_rows.get(strategy_id)
        if strategy_row is None:
            continue
        if str(inv_row.get("platform") or "") != "plugin_kernel":
            continue
        try:
            plugin = _load_plugin(strategy_row)
            resolved = dict(plugin.apply_default_extras(dict((((strategy_row.get("params") or {}).get("extras")) or {}))))
            definition_id = str(resolved.get("definition_id") or "").strip()
            definition = definition_registry.get(definition_id)
            evidence = definition.evidence_contract
            issues: List[str] = []
            if bool(evidence.replay_strategy_required) and not str(resolved.get("replay_strategy_id") or "").strip():
                issues.append("missing_replay_strategy_id")
            if bool(evidence.parity_profile_required) and not str(resolved.get("live_parity_profile_id") or "").strip():
                issues.append("missing_live_parity_profile_id")
            if bool(evidence.parity_snapshot_required) and not bool(resolved.get("enable_live_parity_snapshot", False)):
                issues.append("live_parity_snapshot_not_enabled")
            if bool(evidence.shadow_toggle_required) and "enable_live_shadow_mode" not in resolved:
                issues.append("missing_enable_live_shadow_mode_toggle")
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "status": str(inv_row.get("status") or ""),
                    "definition_id": definition_id,
                    "strategy_family": str(definition.strategy_family),
                    "strategy_archetype": str(definition.strategy_archetype),
                    "evidence_contract_id": str(evidence.contract_id),
                    "replay_harness": str(evidence.replay_harness),
                    "cutover_policy": str(evidence.cutover_policy),
                    "state_shadow_required": bool(evidence.state_shadow_required),
                    "replay_strategy_id": str(resolved.get("replay_strategy_id") or ""),
                    "live_parity_profile_id": str(resolved.get("live_parity_profile_id") or ""),
                    "enable_live_parity_snapshot": bool(resolved.get("enable_live_parity_snapshot", False)),
                    "enable_live_shadow_mode": bool(resolved.get("enable_live_shadow_mode", False)),
                    "rollout_contract_pass": not issues,
                    "issues": issues,
                }
            )
        except Exception as exc:
            error_msg = str(exc).strip()
            if not error_msg:
                error_msg = exc.__class__.__name__
            if error_msg in {"''", '""', "KeyError"}:
                error_msg = "definition_resolution_failed: missing definition_id"
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "status": str(inv_row.get("status") or ""),
                    "definition_id": str(inv_row.get("definition_id") or ""),
                    "strategy_family": "",
                    "strategy_archetype": "",
                    "evidence_contract_id": "",
                    "replay_harness": "",
                    "cutover_policy": "",
                    "state_shadow_required": False,
                    "replay_strategy_id": "",
                    "live_parity_profile_id": "",
                    "enable_live_parity_snapshot": False,
                    "enable_live_shadow_mode": False,
                    "rollout_contract_pass": False,
                    "issues": [error_msg],
                }
            )
    rows.sort(key=lambda row: row["strategy_id"])
    active_rows = [row for row in rows if row["status"] == "active"]
    active_failures = [row["strategy_id"] for row in active_rows if not row["rollout_contract_pass"]]
    state_shadow_required_ids = [row["strategy_id"] for row in active_rows if bool(row["state_shadow_required"])]
    recommendation = "ROLLOUT_CONTRACT_READY" if not active_failures else "BLOCK"
    return {
        "summary": {
            "strategy_count": len(rows),
            "active_strategy_count": len(active_rows),
            "active_rollout_contract_fail_count": len(active_failures),
            "active_state_shadow_required_count": len(state_shadow_required_ids),
        },
        "recommendation": recommendation,
        "active_failing_strategy_ids": active_failures,
        "active_state_shadow_required_strategy_ids": state_shadow_required_ids,
        "strategies": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build option rollout evidence report")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = build_option_strategy_rollout_evidence_report(args.config)
    payload = json.dumps(report, indent=2, sort_keys=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
