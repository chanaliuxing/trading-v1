#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _is_strategy_disabled(config_payload: Dict[str, Any], strategy_id: str) -> bool:
    item = _get_strategy_config_entry(config_payload, strategy_id)
    if not isinstance(item, dict):
        return False
    return bool(item.get("enabled") is False)


def _get_strategy_config_entry(config_payload: Dict[str, Any], strategy_id: str) -> Optional[Dict[str, Any]]:
    strategies = list(config_payload.get("strategies") or [])
    for item in strategies:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() != strategy_id:
            continue
        return item
    return None


def _strategy_points_to_replica(
    *,
    strategy_entry: Optional[Dict[str, Any]],
    expected_module: str,
    expected_class: str,
) -> bool:
    if not isinstance(strategy_entry, dict):
        return False
    module_name = str(strategy_entry.get("module") or "").strip()
    class_name = str(strategy_entry.get("class") or "").strip()
    return module_name == str(expected_module or "").strip() and class_name == str(expected_class or "").strip()


def build_replica_final_enablement_checklist(
    *,
    config_path: Path,
    strategy_id: str,
    expected_replica_module: str,
    expected_replica_class: str,
    preflight_report_path: Path,
    real_session_summary_path: Path,
    batch_report_path: Path,
    isolation_daily_summary_path: Path,
    trace_smoke_gate_path: Path,
    session_bundle_report_path: Path | None,
    max_high_value_isolation_events: int,
    required_green_days: int,
) -> Dict[str, Any]:
    blockers: List[str] = []

    config_payload = _load_yaml(config_path)
    preflight = _load_json(preflight_report_path)
    real_summary = _load_json(real_session_summary_path)
    batch_report = _load_json(batch_report_path)
    isolation_daily = _load_json(isolation_daily_summary_path)
    trace_smoke = _load_json(trace_smoke_gate_path)
    session_bundle = _load_json(session_bundle_report_path) if session_bundle_report_path is not None else {}

    strategy_entry = _get_strategy_config_entry(config_payload, strategy_id)
    strategy_disabled = _is_strategy_disabled(config_payload, strategy_id)
    if not strategy_disabled:
        blockers.append("strategy_not_disabled_in_config")
    strategy_points_to_replica = _strategy_points_to_replica(
        strategy_entry=strategy_entry,
        expected_module=expected_replica_module,
        expected_class=expected_replica_class,
    )
    if not strategy_points_to_replica:
        blockers.append("strategy_not_pointing_to_replica_module_class")

    preflight_ready = bool(preflight.get("ready_for_enablement", False))
    if not preflight_ready:
        blockers.append("preflight_not_green")

    has_any_ready_day = bool(real_summary.get("has_any_ready_day", False))
    if not has_any_ready_day:
        blockers.append("no_ready_real_session_day")

    pipeline_status = str(real_summary.get("pipeline_status") or "").strip()
    if pipeline_status not in {"batch_completed", "batch_green"}:
        blockers.append("real_session_pipeline_not_completed")

    green_days = int(batch_report.get("green_days", 0) or 0)
    if green_days < int(required_green_days):
        blockers.append("insufficient_green_proof_days")

    attestation_green_days = int(batch_report.get("enablement_attestation_green_days", 0) or 0)
    if attestation_green_days < int(required_green_days):
        blockers.append("insufficient_green_attestation_days")

    high_value_isolation_events = int(isolation_daily.get("high_value_event_count", 0) or 0)
    if high_value_isolation_events > int(max_high_value_isolation_events):
        blockers.append("high_value_isolation_events_exceeded")

    trace_smoke_ready = bool(trace_smoke.get("ready_for_trace_parity_capture", False))
    if not trace_smoke_ready:
        blockers.append("trace_smoke_gate_not_ready")

    session_bundle_ready = True
    if session_bundle_report_path is not None:
        session_bundle_ready = bool(session_bundle.get("ready_for_enablement", False))
        if not session_bundle_ready:
            blockers.append("session_bundle_not_green")

    non_green_days = []
    for row in list(batch_report.get("rows") or []):
        if bool(row.get("enablement_attestation_green")):
            continue
        non_green_days.append(
            {
                "day": row.get("day"),
                "status": row.get("status"),
                "overall_green": row.get("overall_green"),
                "enablement_attestation_green": row.get("enablement_attestation_green"),
            }
        )

    checklist = {
        "config_path": str(config_path),
        "strategy_id": strategy_id,
        "expected_replica_module": expected_replica_module,
        "expected_replica_class": expected_replica_class,
        "preflight_report_path": str(preflight_report_path),
        "real_session_summary_path": str(real_session_summary_path),
        "batch_report_path": str(batch_report_path),
        "isolation_daily_summary_path": str(isolation_daily_summary_path),
        "trace_smoke_gate_path": str(trace_smoke_gate_path),
        "session_bundle_report_path": str(session_bundle_report_path) if session_bundle_report_path is not None else None,
        "max_high_value_isolation_events": int(max_high_value_isolation_events),
        "required_green_days": int(required_green_days),
        "checks": {
            "strategy_disabled_in_config": strategy_disabled,
            "strategy_points_to_replica_module_class": strategy_points_to_replica,
            "preflight_ready_for_enablement": preflight_ready,
            "has_any_ready_real_session_day": has_any_ready_day,
            "real_session_pipeline_completed": pipeline_status in {"batch_completed", "batch_green"},
            "sufficient_green_proof_days": green_days >= int(required_green_days),
            "sufficient_green_attestation_days": attestation_green_days >= int(required_green_days),
            "high_value_isolation_events_within_limit": high_value_isolation_events <= int(max_high_value_isolation_events),
            "trace_smoke_gate_ready_for_parity_capture": trace_smoke_ready,
            "session_bundle_ready_for_enablement": session_bundle_ready,
        },
        "observed": {
            "pipeline_status": pipeline_status or None,
            "ready_days": list(real_summary.get("ready_days") or []),
            "green_days": green_days,
            "attestation_green_days": attestation_green_days,
            "high_value_isolation_event_count": high_value_isolation_events,
            "trace_smoke_row_count": int(trace_smoke.get("observed", {}).get("row_count", 0) or 0),
            "trace_smoke_trace_count": int(trace_smoke.get("observed", {}).get("trace_count", 0) or 0),
            "trace_smoke_decision_symbol_count": int(trace_smoke.get("observed", {}).get("decision_symbol_count", 0) or 0),
            "session_bundle_error_count": int(len(list(session_bundle.get("errors") or []))) if session_bundle_report_path is not None else 0,
            "non_green_days": non_green_days,
            "needs_more_real_samples": bool(real_summary.get("needs_more_real_samples", False)),
        },
        "blockers": blockers,
        "ready_to_enable": not blockers,
    }
    return checklist


def main() -> int:
    parser = argparse.ArgumentParser(description="Build final machine checklist for replica enablement.")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--strategy-id", default="index_super_win_001")
    parser.add_argument("--expected-replica-module", default="strategies.index_super_win_replica_strategy")
    parser.add_argument("--expected-replica-class", default="IndexSuperWinReplicaStrategy")
    parser.add_argument(
        "--preflight-report",
        default=str(ROOT / "runtime" / "state" / "index_super_win_replica_preflight.json"),
    )
    parser.add_argument(
        "--real-session-summary",
        default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / "real_session_proof_daily_summary.json"),
    )
    parser.add_argument(
        "--batch-report",
        default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / "real_session_dual_proof_batch_report.json"),
    )
    parser.add_argument(
        "--isolation-daily-summary",
        default=str(ROOT / "runtime" / "state" / "multi_strategy_isolation_daily_summary.json"),
    )
    parser.add_argument(
        "--trace-smoke-gate",
        default=str(ROOT / "runtime" / "state" / "index_super_win_trace_smoke_gate.json"),
    )
    parser.add_argument(
        "--session-bundle-report",
        default="",
    )
    parser.add_argument(
        "--output-path",
        default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / "final_enablement_checklist.json"),
    )
    parser.add_argument("--required-green-days", type=int, default=3)
    parser.add_argument("--max-high-value-isolation-events", type=int, default=0)
    args = parser.parse_args()

    report = build_replica_final_enablement_checklist(
        config_path=Path(args.config),
        strategy_id=str(args.strategy_id),
        expected_replica_module=str(args.expected_replica_module),
        expected_replica_class=str(args.expected_replica_class),
        preflight_report_path=Path(args.preflight_report),
        real_session_summary_path=Path(args.real_session_summary),
        batch_report_path=Path(args.batch_report),
        isolation_daily_summary_path=Path(args.isolation_daily_summary),
        trace_smoke_gate_path=Path(args.trace_smoke_gate),
        session_bundle_report_path=Path(args.session_bundle_report) if str(args.session_bundle_report).strip() else None,
        max_high_value_isolation_events=int(args.max_high_value_isolation_events),
        required_green_days=int(args.required_green_days),
    )
    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(output_path)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("ready_to_enable")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
