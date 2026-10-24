from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_stocks_kernel_launch_go_no_go_gate(
    *,
    readiness_report: dict[str, Any],
    matrix_report: dict[str, Any],
    audit_summary: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    if not bool(readiness_report.get("ready", False)):
        blockers.append("runtime_readiness_not_ready")
    blocking_checks = {
        str(check.get("check_id") or "")
        for check in list(readiness_report.get("blocking_checks") or [])
        if isinstance(check, dict)
    }
    if "symbol_kill_switches" in blocking_checks:
        blockers.append("active_symbol_kill_switches")
    if "entry_freezes" in blocking_checks:
        blockers.append("entry_freezes_present")
    if "protection_invariants" in blocking_checks:
        blockers.append("unsafe_live_positions_present")

    if not bool(matrix_report.get("passed", False)):
        blockers.append("regression_matrix_failed")
    scenario_count = int(matrix_report.get("scenario_count") or 0)
    if scenario_count < 2:
        blockers.append("regression_matrix_insufficient")

    blocked_events = int(audit_summary.get("blocked_events") or 0)
    kill_switch_events = int(audit_summary.get("kill_switch_events") or 0)
    protection_events = int(audit_summary.get("protection_events") or 0)
    if kill_switch_events > 0:
        warnings.append("recent_kill_switch_events_present")
    if protection_events > 0:
        warnings.append("recent_protection_events_present")
    if blocked_events > 0:
        warnings.append("recent_blocked_events_present")

    checklist = [
        {"item": "runtime_readiness_green", "pass": bool(readiness_report.get("ready", False))},
        {"item": "no_active_symbol_kill_switches", "pass": "symbol_kill_switches" not in blocking_checks},
        {"item": "no_entry_freezes", "pass": "entry_freezes" not in blocking_checks},
        {"item": "no_unsafe_live_positions", "pass": "protection_invariants" not in blocking_checks},
        {"item": "regression_matrix_green", "pass": bool(matrix_report.get("passed", False))},
        {"item": "regression_matrix_scenarios_ge_2", "pass": scenario_count >= 2},
        {"item": "recent_kill_switch_events_eq_0", "pass": kill_switch_events == 0},
        {"item": "recent_protection_events_eq_0", "pass": protection_events == 0},
    ]

    recommendation = "GO" if not blockers else "BLOCK"
    return {
        "generated_at": datetime.now().isoformat(),
        "recommendation": recommendation,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "warning_count": len(warnings),
        "warnings": warnings,
        "checklist": checklist,
        "readiness_reason": str(readiness_report.get("reason") or ""),
        "matrix_reason": str(matrix_report.get("reason") or ""),
        "audit_rows": int(audit_summary.get("rows") or 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build stocks kernel launch GO/BLOCK gate")
    parser.add_argument("--readiness-report", default=str(ROOT / "runtime" / "state" / "stocks_kernel_readiness.json"))
    parser.add_argument("--matrix-report", default=str(ROOT / "runtime" / "state" / "stocks_kernel_regression_matrix.json"))
    parser.add_argument("--audit-summary", default=str(ROOT / "runtime" / "state" / "stocks_kernel_audit_summary.json"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    report = build_stocks_kernel_launch_go_no_go_gate(
        readiness_report=_load_json_if_exists(Path(args.readiness_report).expanduser()),
        matrix_report=_load_json_if_exists(Path(args.matrix_report).expanduser()),
        audit_summary=_load_json_if_exists(Path(args.audit_summary).expanduser()),
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        return
    print(payload)


if __name__ == "__main__":
    main()
