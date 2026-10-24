from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from strategies.options_kernel.config_contract import validate_option_strategy_config_row
from strategies.options_kernel.inventory import build_option_strategy_inventory
from tool.report.build_option_assembly_adapter_report import build_option_assembly_adapter_report
from tool.report.build_option_platform_coverage_report import build_option_platform_coverage_report


ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(path: str | Path) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def build_option_platform_admission_gate(config_path: str | Path) -> Dict[str, Any]:
    config_payload = _load_yaml(config_path)
    inventory = build_option_strategy_inventory(config_payload)
    assembly = build_option_assembly_adapter_report(config_path)
    coverage = build_option_platform_coverage_report(config_path)

    active_rows = {
        str(row.get("strategy_id") or ""): row
        for row in list(inventory.get("strategies") or [])
        if str(row.get("status") or "") == "active"
    }
    config_rows = {
        str(row.get("id") or ""): row
        for row in list(config_payload.get("strategies") or [])
        if isinstance(row, dict)
    }
    active_validation: List[Dict[str, Any]] = []
    for strategy_id in sorted(active_rows.keys()):
        row = config_rows.get(strategy_id)
        if row is None:
            active_validation.append(
                {"strategy_id": strategy_id, "issues": ["missing_config_row"], "pass": False, "resolved_identity": {}}
            )
            continue
        active_validation.append(validate_option_strategy_config_row(row))

    active_failures = [row["strategy_id"] for row in active_validation if not row["pass"]]
    assembly_failures = list(assembly.get("failing_strategy_ids") or [])
    legacy_active_rows = [
        row["strategy_id"]
        for row in list(inventory.get("strategies") or [])
        if str(row.get("status") or "") == "active" and str(row.get("platform") or "") != "plugin_kernel"
    ]

    reasons: List[str] = []
    next_actions: List[str] = []
    if int(coverage.get("summary", {}).get("distinct_archetype_count", 0) or 0) < 3:
        reasons.append("insufficient_strategy_archetype_coverage")
        next_actions.append("add_or_restore_third_definition_driven_archetype_sample")
    if int(assembly.get("summary", {}).get("assembly_adapter_fail_count", 0) or 0) > 0:
        reasons.append("active_assembly_adapter_failures_present")
        next_actions.append("fix_active_strategy_assembly_adapter_failures")
    if active_failures:
        reasons.append("active_config_contract_failures_present")
        next_actions.append("fix_active_strategy_config_contract_failures")
    if legacy_active_rows:
        reasons.append("legacy_active_option_paths_present")
        next_actions.append("migrate_or_retire_legacy_active_option_paths")

    recommendation = "BLOCK"
    if not reasons:
        recommendation = "PLATFORM_ADMISSION_REVIEW_ALLOWED"
        next_actions.append("run_replay_shadow_and_cutover_gates_per_strategy_before_live_enablement")

    return {
        "summary": {
            "active_option_strategy_count": int(inventory.get("summary", {}).get("active_count", 0) or 0),
            "active_plugin_kernel_count": len(active_rows) - len(legacy_active_rows),
            "active_validation_fail_count": len(active_failures),
            "legacy_active_count": len(legacy_active_rows),
            "distinct_archetype_count": int(coverage.get("summary", {}).get("distinct_archetype_count", 0) or 0),
            "coverage_goal_met": bool(coverage.get("summary", {}).get("coverage_goal_met", False)),
        },
        "recommendation": recommendation,
        "platform_admission_blocked": recommendation != "PLATFORM_ADMISSION_REVIEW_ALLOWED",
        "reasons": reasons,
        "next_actions": next_actions,
        "inventory_summary": inventory.get("summary", {}),
        "assembly_summary": assembly.get("summary", {}),
        "coverage_summary": coverage.get("summary", {}),
        "assembly_failing_strategy_ids": assembly_failures,
        "active_config_failing_strategy_ids": active_failures,
        "legacy_active_strategy_ids": legacy_active_rows,
        "active_strategy_validations": active_validation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build option platform admission gate report")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = build_option_platform_admission_gate(args.config)
    payload = json.dumps(report, indent=2, sort_keys=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
