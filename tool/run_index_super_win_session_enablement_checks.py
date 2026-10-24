#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.index_super_win_replica_preflight import (
    _default_audit_root,
    _default_baseline_path,
    _default_diag_snapshot_path,
    build_preflight_report,
)
from tool.index_super_win_session_bundle_preflight import build_session_bundle_report
from tool.replay.build_replica_final_enablement_checklist import build_replica_final_enablement_checklist


def _load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _resolve_runtime_snapshot_path(config_path: Path) -> Path | None:
    payload = _load_yaml(config_path)
    raw = str(payload.get("runtime_snapshot_path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def run_session_enablement_checks(
    *,
    config_paths: List[Path],
    broker_positions_path: Path,
    bundle_output_path: Path | None,
    per_instance_output_dir: Path | None,
    final_checklist_output_path: Path | None,
    config_path: Path,
    strategy_id: str,
    expected_replica_module: str,
    expected_replica_class: str,
    real_session_summary_path: Path,
    batch_report_path: Path,
    isolation_daily_summary_path: Path,
    trace_smoke_gate_path: Path,
    required_green_days: int,
    max_high_value_isolation_events: int,
) -> Dict[str, Any]:
    bundle_report = build_session_bundle_report(config_paths=config_paths)
    if bundle_output_path is not None:
        bundle_output_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_output_path.write_text(json.dumps(bundle_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    session_reports: List[Dict[str, Any]] = []
    overall_ready = bool(bundle_report.get("ready_for_enablement", False))
    for session_config_path in config_paths:
        runtime_snapshot_path = _resolve_runtime_snapshot_path(session_config_path)
        report = build_preflight_report(
            manifest_path=session_config_path,
            baseline_path=_default_baseline_path(),
            runtime_snapshot_path=runtime_snapshot_path,
            broker_positions_path=broker_positions_path,
            diag_snapshot_path=_default_diag_snapshot_path(),
            audit_root=_default_audit_root(),
        )
        session_id = str(report.get("observed", {}).get("strategy_id") or _load_yaml(session_config_path).get("strategy_id") or session_config_path.stem)
        report["session_config_path"] = str(session_config_path)
        report["session_runtime_snapshot_path"] = str(runtime_snapshot_path) if runtime_snapshot_path is not None else None
        if per_instance_output_dir is not None:
            per_instance_output_dir.mkdir(parents=True, exist_ok=True)
            out_path = per_instance_output_dir / f"{session_id}.json"
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            report["report_path"] = str(out_path)
        session_reports.append(report)
        overall_ready = overall_ready and bool(report.get("ready_for_enablement", False))

    final_report: Dict[str, Any] | None = None
    if final_checklist_output_path is not None and bundle_output_path is not None:
        # Preserve the existing final checklist contract and add the bundle gate.
        merged_preflight_path = None
        if per_instance_output_dir is not None and session_reports:
            merged_preflight_path = per_instance_output_dir / "_aggregate_ready.json"
            aggregate_payload = {"ready_for_enablement": all(bool(item.get("ready_for_enablement", False)) for item in session_reports)}
            merged_preflight_path.write_text(json.dumps(aggregate_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        elif session_reports:
            tmp_payload = {"ready_for_enablement": all(bool(item.get("ready_for_enablement", False)) for item in session_reports)}
            merged_preflight_path = ROOT / "runtime" / "state" / "index_super_win_session_preflight_aggregate.json"
            merged_preflight_path.parent.mkdir(parents=True, exist_ok=True)
            merged_preflight_path.write_text(json.dumps(tmp_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if merged_preflight_path is not None:
            final_report = build_replica_final_enablement_checklist(
                config_path=config_path,
                strategy_id=strategy_id,
                expected_replica_module=expected_replica_module,
                expected_replica_class=expected_replica_class,
                preflight_report_path=merged_preflight_path,
                real_session_summary_path=real_session_summary_path,
                batch_report_path=batch_report_path,
                isolation_daily_summary_path=isolation_daily_summary_path,
                trace_smoke_gate_path=trace_smoke_gate_path,
                session_bundle_report_path=bundle_output_path,
                max_high_value_isolation_events=max_high_value_isolation_events,
                required_green_days=required_green_days,
            )
            final_checklist_output_path.parent.mkdir(parents=True, exist_ok=True)
            final_checklist_output_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            overall_ready = overall_ready and bool(final_report.get("ready_to_enable", False))

    return {
        "ready_for_enablement": overall_ready,
        "bundle_report_path": str(bundle_output_path) if bundle_output_path is not None else None,
        "bundle_ready_for_enablement": bool(bundle_report.get("ready_for_enablement", False)),
        "session_reports": session_reports,
        "final_checklist_path": str(final_checklist_output_path) if final_checklist_output_path is not None else None,
        "final_ready_to_enable": None if final_report is None else bool(final_report.get("ready_to_enable", False)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bundle + per-session + final enablement checks for QQQ relay sessions.")
    parser.add_argument("--session-config", action="append", required=True, dest="session_configs")
    parser.add_argument("--broker-positions", required=True)
    parser.add_argument("--bundle-output-path", default=str(ROOT / "runtime" / "state" / "index_super_win_session_bundle_preflight.json"))
    parser.add_argument("--per-instance-output-dir", default=str(ROOT / "runtime" / "state" / "index_super_win_session_preflight"))
    parser.add_argument("--final-checklist-output-path", default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / "final_enablement_checklist_sessions.json"))
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--strategy-id", default="index_super_win_001")
    parser.add_argument("--expected-replica-module", default="strategies.index_super_win_replica_strategy")
    parser.add_argument("--expected-replica-class", default="IndexSuperWinReplicaStrategy")
    parser.add_argument("--real-session-summary", default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / "real_session_proof_daily_summary.json"))
    parser.add_argument("--batch-report", default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / "real_session_dual_proof_batch_report.json"))
    parser.add_argument("--isolation-daily-summary", default=str(ROOT / "runtime" / "state" / "multi_strategy_isolation_daily_summary.json"))
    parser.add_argument("--trace-smoke-gate", default=str(ROOT / "runtime" / "state" / "index_super_win_trace_smoke_gate.json"))
    parser.add_argument("--required-green-days", type=int, default=3)
    parser.add_argument("--max-high-value-isolation-events", type=int, default=0)
    args = parser.parse_args()

    report = run_session_enablement_checks(
        config_paths=[Path(item) for item in list(args.session_configs or [])],
        broker_positions_path=Path(args.broker_positions),
        bundle_output_path=Path(args.bundle_output_path) if str(args.bundle_output_path).strip() else None,
        per_instance_output_dir=Path(args.per_instance_output_dir) if str(args.per_instance_output_dir).strip() else None,
        final_checklist_output_path=Path(args.final_checklist_output_path) if str(args.final_checklist_output_path).strip() else None,
        config_path=Path(args.config),
        strategy_id=str(args.strategy_id),
        expected_replica_module=str(args.expected_replica_module),
        expected_replica_class=str(args.expected_replica_class),
        real_session_summary_path=Path(args.real_session_summary),
        batch_report_path=Path(args.batch_report),
        isolation_daily_summary_path=Path(args.isolation_daily_summary),
        trace_smoke_gate_path=Path(args.trace_smoke_gate),
        required_green_days=int(args.required_green_days),
        max_high_value_isolation_events=int(args.max_high_value_isolation_events),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("ready_for_enablement", False)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
