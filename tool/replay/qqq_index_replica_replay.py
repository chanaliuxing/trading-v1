#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.strategy_runtime_v2.parity_recorder import compare_event_streams


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_baseline_contract(baseline: Dict[str, Any]) -> Dict[str, Any]:
    required_top_level = [
        "git_head",
        "parity_fields",
        "minimum_replay_fixtures",
        "behavior_freeze_contract",
        "state_machine_upgrade_contract",
    ]
    missing = [name for name in required_top_level if not baseline.get(name)]
    if missing:
        raise ValueError(f"qqq baseline missing required fields: {', '.join(sorted(missing))}")
    freeze = dict(baseline.get("behavior_freeze_contract") or {})
    upgrade = dict(baseline.get("state_machine_upgrade_contract") or {})
    if str(freeze.get("strategy_id") or "").strip() != "qqq_intraday_001":
        raise ValueError("qqq baseline behavior_freeze_contract.strategy_id must be qqq_intraday_001")
    if str(freeze.get("qqq_cutover_policy") or "").strip() != "last_strategy_to_cut_over":
        raise ValueError("qqq baseline must require qqq_cutover_policy=last_strategy_to_cut_over")
    if not bool(freeze.get("rollback_required")):
        raise ValueError("qqq baseline must require rollback_required=true")
    if not bool(upgrade.get("unified_kernel_required")):
        raise ValueError("qqq baseline must require unified_kernel_required=true")
    states = {str(item).strip() for item in list(upgrade.get("independent_state_machines") or []) if str(item).strip()}
    missing_states = sorted({"runtime", "order", "flatten", "protection"} - states)
    if missing_states:
        raise ValueError(f"qqq baseline missing state machines: {', '.join(missing_states)}")
    return {
        "freeze_contract_ok": True,
        "state_machine_contract_ok": True,
        "independent_state_machines": sorted(states),
    }


def build_report(
    *,
    baseline_path: Path,
    expected_trace_path: Path,
    actual_trace_path: Path,
    qty_tolerance: float,
) -> Dict[str, Any]:
    baseline = _load_json(baseline_path)
    baseline_validation = validate_baseline_contract(baseline)
    expected_trace = _load_json(expected_trace_path)
    actual_trace = _load_json(actual_trace_path)
    comparison = compare_event_streams(
        expected_trace.get("events") or [],
        actual_trace.get("events") or [],
        qty_tolerance=qty_tolerance,
    )
    return {
        "baseline_git_head": baseline.get("git_head"),
        "strategy_id": baseline.get("strategy_id"),
        "baseline_validation": baseline_validation,
        "behavior_freeze_contract": dict(baseline.get("behavior_freeze_contract") or {}),
        "parity_fields": list(baseline.get("parity_fields") or []),
        "minimum_replay_fixtures": list(baseline.get("minimum_replay_fixtures") or []),
        "state_machine_upgrade_contract": dict(baseline.get("state_machine_upgrade_contract") or {}),
        "expected_trace": str(expected_trace_path),
        "actual_trace": str(actual_trace_path),
        "comparison": comparison,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare qqq baseline trace vs index replica trace")
    parser.add_argument(
        "--baseline",
        default=str(ROOT / "docs" / "baselines" / "qqq_intraday_001_replica_baseline.json"),
    )
    parser.add_argument("--expected-trace", required=True)
    parser.add_argument("--actual-trace", required=True)
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(
        baseline_path=Path(args.baseline),
        expected_trace_path=Path(args.expected_trace),
        actual_trace_path=Path(args.actual_trace),
        qty_tolerance=float(args.qty_tolerance),
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if args.json or True:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("comparison", {}).get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
