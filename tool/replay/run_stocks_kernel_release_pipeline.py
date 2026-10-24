#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.report.build_stocks_kernel_audit_daily_summary import build_daily_summary
from tool.report.build_stocks_kernel_launch_go_no_go_gate import build_stocks_kernel_launch_go_no_go_gate
from tool.replay.run_stocks_kernel_trader_parity_gate import build_trader_parity_gate
from tool.replay.run_stocks_kernel_readiness import _run as run_readiness_report
from tool.replay.run_stocks_kernel_regression_matrix import _run as run_matrix_report


def _load_json(path: str) -> dict[str, Any]:
    if not str(path or "").strip():
        return {}
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def _build_unavailable_trader_parity_gate(*, reason: str) -> dict[str, Any]:
    reason_text = str(reason or "unavailable").strip() or "unavailable"
    check_name = f"trader_parity_gate_{reason_text}"
    return {
        "ok": False,
        "recommendation": "HOLD",
        "failed_checks": [check_name],
        "checks": [
            {
                "name": check_name,
                "passed": False,
                "detail": {"reason": reason_text},
            }
        ],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _extract_trader_parity_config(config: dict[str, Any]) -> dict[str, Any]:
    extras = dict(config.get("extras") or {})
    payload = dict(extras.get("trader_parity_gate") or {})
    policy_params = dict(extras.get("policy_params") or {})
    strategy_requires_parity = bool(policy_params.get("rule_engine_parity"))
    enabled = bool(payload.get("enabled"))
    required = bool(payload.get("required", strategy_requires_parity))
    return {
        "enabled": enabled,
        "required": required,
        "identity_report_path": str(payload.get("identity_report_path") or "").strip(),
        "daily_summary_report_path": str(payload.get("daily_summary_report_path") or "").strip(),
        "legacy_strategy_id": str(payload.get("legacy_strategy_id") or "qqq_intraday_001").strip(),
        "kernel_strategy_id": str(payload.get("kernel_strategy_id") or config.get("strategy_id") or "").strip(),
        "min_allow_match_ratio": max(0.98, float(payload.get("min_allow_match_ratio", 0.98))),
        "min_side_match_ratio": max(0.98, float(payload.get("min_side_match_ratio", 0.98))),
        "min_reason_match_ratio": max(0.98, float(payload.get("min_reason_match_ratio", 0.98))),
        "min_exec_symbol_match_ratio": max(1.0, float(payload.get("min_exec_symbol_match_ratio", 1.0))),
        "max_fill_multiple": max(0.1, float(payload.get("max_fill_multiple", 2.0))),
        "max_open_trigger_count": max(0, int(payload.get("max_open_trigger_count", 0))),
        "max_position_notional_abs_usd": max(0.0, float(payload.get("max_position_notional_abs_usd", 0.0))),
    }


def run_stocks_kernel_release_pipeline(
    *,
    config: dict[str, Any],
    runtime_state: dict[str, Any],
    scenarios: list[dict[str, Any]],
    audit_root: Path,
    audit_date: str,
    output_dir: Path,
) -> dict[str, Any]:
    def _stage_count(summary: dict[str, Any], stage: str) -> int:
        top = list(summary.get("top_stages") or [])
        for item in top:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[0]) == stage:
                try:
                    return int(item[1])
                except Exception:
                    return 0
        return 0

    readiness = asyncio.run(run_readiness_report(config=config, runtime_state=runtime_state))
    matrix = asyncio.run(run_matrix_report(config=config, scenarios_payload=list(scenarios or [])))
    audit_summary = build_daily_summary(audit_root=audit_root, date=audit_date)
    trader_parity_cfg = _extract_trader_parity_config(config)
    trader_parity_gate: dict[str, Any] = {}
    if trader_parity_cfg["enabled"]:
        if not trader_parity_cfg["identity_report_path"] or not trader_parity_cfg["daily_summary_report_path"]:
            trader_parity_gate = _build_unavailable_trader_parity_gate(reason="missing_report_path")
        else:
            try:
                identity_report = _load_json(trader_parity_cfg["identity_report_path"])
                daily_summary_report = _load_json(trader_parity_cfg["daily_summary_report_path"])
            except Exception as exc:
                trader_parity_gate = _build_unavailable_trader_parity_gate(reason=f"load_error:{type(exc).__name__}")
            else:
                trader_parity_gate = build_trader_parity_gate(
                    identity_report=identity_report,
                    daily_summary_report=daily_summary_report,
                    legacy_strategy_id=trader_parity_cfg["legacy_strategy_id"],
                    kernel_strategy_id=trader_parity_cfg["kernel_strategy_id"],
                    min_allow_match_ratio=trader_parity_cfg["min_allow_match_ratio"],
                    min_side_match_ratio=trader_parity_cfg["min_side_match_ratio"],
                    min_reason_match_ratio=trader_parity_cfg["min_reason_match_ratio"],
                    min_exec_symbol_match_ratio=trader_parity_cfg["min_exec_symbol_match_ratio"],
                    max_fill_multiple=trader_parity_cfg["max_fill_multiple"],
                    max_open_trigger_count=trader_parity_cfg["max_open_trigger_count"],
                    max_position_notional_abs_usd=trader_parity_cfg["max_position_notional_abs_usd"],
                )
    elif trader_parity_cfg["required"]:
        trader_parity_gate = _build_unavailable_trader_parity_gate(reason="required_but_disabled")
    gate = build_stocks_kernel_launch_go_no_go_gate(
        readiness_report=readiness,
        matrix_report=matrix,
        audit_summary={
            "rows": int(audit_summary.get("row_count") or 0),
            "blocked_events": _stage_count(audit_summary, "entry_blocked_ownership"),
            "kill_switch_events": _stage_count(audit_summary, "symbol_kill_switch_activated"),
            "protection_events": _stage_count(audit_summary, "protection_lifecycle_updated"),
        },
    )
    checklist = list(gate.get("checklist") or [])
    blockers = list(gate.get("blockers") or [])
    if trader_parity_cfg["enabled"] or trader_parity_cfg["required"]:
        trader_parity_ok = str(trader_parity_gate.get("recommendation") or "") == "GO"
        checklist.append({"item": "trader_parity_gate_green", "pass": trader_parity_ok})
        if not trader_parity_ok:
            blockers.append("trader_parity_gate_failed")
        gate = {
            **gate,
            "recommendation": "GO" if not blockers else "BLOCK",
            "blocker_count": len(blockers),
            "blockers": blockers,
            "checklist": checklist,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    readiness_path = output_dir / "stocks_kernel_readiness.json"
    matrix_path = output_dir / "stocks_kernel_regression_matrix.json"
    audit_path = output_dir / "stocks_kernel_audit_summary.json"
    gate_path = output_dir / "stocks_kernel_launch_go_no_go_gate.json"
    trader_parity_path = output_dir / "stocks_kernel_trader_parity_gate.json"
    _write_json(readiness_path, readiness)
    _write_json(matrix_path, matrix)
    _write_json(audit_path, audit_summary)
    _write_json(gate_path, gate)
    if trader_parity_cfg["enabled"] or trader_parity_cfg["required"]:
        _write_json(trader_parity_path, trader_parity_gate)

    report = {
        "generated_at": datetime.now().isoformat(),
        "audit_date": audit_date,
        "recommendation": str(gate.get("recommendation") or "BLOCK"),
        "pipeline_green": str(gate.get("recommendation") or "") == "GO",
        "readiness_report_path": str(readiness_path),
        "matrix_report_path": str(matrix_path),
        "audit_summary_path": str(audit_path),
        "launch_gate_report_path": str(gate_path),
        "trader_parity_gate_enabled": bool(trader_parity_cfg["enabled"]),
        "trader_parity_gate_required": bool(trader_parity_cfg["required"]),
    }
    if trader_parity_cfg["enabled"] or trader_parity_cfg["required"]:
        report["trader_parity_gate_report_path"] = str(trader_parity_path)
    report_path = output_dir / "stocks_kernel_release_pipeline.json"
    _write_json(report_path, report)
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run stocks kernel release pipeline")
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime-state", default="")
    parser.add_argument("--scenarios", default="")
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--audit-date", default=datetime.now().date().isoformat())
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "state" / "stocks_kernel_release_pipeline"))
    args = parser.parse_args()

    config = _load_json(args.config)
    runtime_state = _load_json(args.runtime_state) if str(args.runtime_state).strip() else {}
    scenarios_payload = _load_json(args.scenarios) if str(args.scenarios).strip() else []
    if not isinstance(scenarios_payload, list):
        raise SystemExit("--scenarios must be a JSON array when provided")
    report = run_stocks_kernel_release_pipeline(
        config=config,
        runtime_state=runtime_state,
        scenarios=list(scenarios_payload),
        audit_root=Path(args.audit_root).expanduser(),
        audit_date=str(args.audit_date),
        output_dir=Path(args.output_dir).expanduser(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("pipeline_green")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
