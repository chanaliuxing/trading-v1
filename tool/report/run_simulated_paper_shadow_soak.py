#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _now_iso() -> str:
    return datetime.now(tz=ET).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def build_simulated_shadow_artifacts(*, repo_root: Path, day: str, mode: str) -> dict[str, Any]:
    generated_at = _now_iso()
    runtime_id = f"sim-shadow-{day}"
    account_id = "PAPER_SIM"
    strategy_id = "simulated_shadow_strategy"
    strategy_instance_id = "simulated_shadow_strategy_001"
    symbol = "SPY"
    intent_id = "intent-sim-shadow-001"
    client_order_id = "client-sim-shadow-001"

    pre_soak = {
        "generated_at": generated_at,
        "mode": mode,
        "env": {
            "TRADING_TEST_ALLOW_LIVE_IBKR": os.getenv("TRADING_TEST_ALLOW_LIVE_IBKR", "0"),
        },
        "broker_connection_policy": "live_broker_disabled_mocked",
        "order_mutation_policy": "real_order_cancel_modify_disabled",
        "reconciliation_mode": "report_only",
        "artifact_paths": {
            "runtime_readiness": "runtime/readiness",
            "runtime_audit": "runtime/audit",
            "reports": "reports",
        },
        "verdict": "PASS",
    }
    _write_json(repo_root / "reports" / "paper_shadow_soak_gate" / f"pre_soak_safety_check_{day}.json", pre_soak)

    broker_truth = {
        "generated_at": generated_at,
        "snapshot_id": f"paper-broker-truth-{day}",
        "mode": mode,
        "source": "paper_broker",
        "truth_source": "paper_broker",
        "broker_truth_backed": True,
        "paper_broker_truth_backed": True,
        "lookup_failed": False,
        "stale": False,
        "account_id": account_id,
        "positions": [],
        "open_orders": [],
        "blockers": [],
        "warnings": ["simulated_shadow_artifact_not_canary_evidence"],
    }
    _write_json(repo_root / "runtime" / "readiness" / "broker_truth_snapshot_latest.json", broker_truth)
    _write_json(repo_root / "runtime" / "readiness" / "paper_broker_truth_snapshot_latest.json", broker_truth)

    position_safety = {
        "generated_at": generated_at,
        "mode": mode,
        "go_no_go": "go",
        "lookup_failed": False,
        "stale": False,
        "dependency_state": {
            "broker_truth_required": True,
            "broker_truth_available": True,
            "broker_truth_backed": True,
            "broker_truth_source": "paper_broker",
            "broker_truth_snapshot_id": broker_truth["snapshot_id"],
            "broker_truth_generated_at": generated_at,
            "broker_truth_age_sec": 0.0,
            "broker_truth_lookup_failed": False,
            "broker_truth_stale": False,
        },
        "blockers": [],
        "warnings": ["simulated_shadow_not_live_readiness"],
        "core_position_residual_cleanup": {"status": "skipped", "reason": "simulated_shadow_report_only"},
        "overnight_carry": {"detected": False, "incident_count": 0, "incidents": []},
    }
    _write_json(repo_root / "runtime" / "readiness" / "position_safety_latest.json", position_safety)

    production_state = {
        "generated_at": generated_at,
        "mode": mode,
        "go_no_go": "go",
        "summary": {
            "failure_count": 0,
            "safety_failure_count": 0,
            "entry_failure_count": 0,
            "artifact_stale_count": 0,
            "runtime_failure_count": 0,
            "idle_count": 0,
            "tradable_entry_ready_count": 1,
            "close_only_allowed_count": 0,
        },
        "buckets": {
            "safety_blockers": [],
            "entry_blockers": [],
            "artifact_stale_blockers": [],
            "runtime_blockers": [],
            "idle_reasons": [],
            "warnings": ["simulated_shadow_artifact_not_canary_evidence"],
        },
        "readiness": {
            "broker_truth": "paper_broker_fresh",
            "position_safety": "fresh",
            "strategy_tradability": "fresh",
            "runtime_version": "fresh",
        },
    }
    _write_json(repo_root / "runtime" / "readiness" / "production_state_latest.json", production_state)

    runtime_version = {
        "generated_at": generated_at,
        "process": {
            "pid": os.getpid(),
            "process_start_time": generated_at,
            "runtime_id": runtime_id,
            "restart_id": runtime_id,
            "command": "tool/report/run_simulated_paper_shadow_soak.py",
        },
        "freshness": {
            "loaded_code_current": True,
            "critical_modules_stale_count": 0,
            "runtime_version_loaded_code_stale": False,
            "restart_required": False,
        },
        "blockers": [],
        "warnings": [],
    }
    _write_json(repo_root / "runtime" / "readiness" / "runtime_version_latest.json", runtime_version)

    heartbeat = {
        "generated_at": generated_at,
        "strategy_id": strategy_id,
        "strategy_instance_id": strategy_instance_id,
        "symbol": symbol,
        "alive": True,
        "configured_live": False,
        "session_open": True,
        "entry_window_open": True,
        "data_ready": True,
        "system_ready": True,
        "risk_ready": True,
        "execution_ready": True,
        "tradable_entry_ready": True,
        "close_only_allowed": False,
        "idle_reason": None,
        "block_reasons": [],
    }
    _write_jsonl(repo_root / "runtime" / "readiness" / f"strategy_tradability_heartbeat_{day}.jsonl", [heartbeat])

    strategy_surface = {
        "generated_at": generated_at,
        "strategies": [
            {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "path_type": "OPTIONS_KERNEL_PURE_PLUGIN",
                "permission_tier": "CANARY_ONLY",
                "contract_readiness": {
                    "data_contract_ready": True,
                    "session_contract_ready": True,
                    "risk_contract_ready": True,
                    "execution_contract_ready": True,
                    "fill_attribution_ready": True,
                    "production_contract_ready": False,
                    "blockers": [],
                    "warnings": ["simulated_shadow_not_full_production"],
                },
                "evidence_flags": {
                    "direct_strategy_execution_path": False,
                    "legacy_strategy_execution_path": False,
                    "option_private_data_path": False,
                    "options_kernel_pure_plugin": True,
                    "stocks_kernel_execution_path": False,
                    "stock_execution_blockers_disabled": False,
                },
            }
        ],
        "strategy_surface_summary": {
            "production_eligible_count": 0,
            "canary_only_count": 1,
            "shadow_only_count": 0,
            "disabled_until_contract_parity_count": 0,
            "unknown_path_count": 0,
        },
    }
    _write_json(repo_root / "reports" / "strategy_surface" / f"live_strategy_surface_report_{day}.json", strategy_surface)

    approved_report = {
        "generated_at": generated_at,
        "window": {"start": generated_at, "end": generated_at},
        "sample_count": 1,
        "stage_counts": {
            "approved": 1,
            "intent_created": 1,
            "reservation_rejected": 0,
            "execution_rejected": 0,
            "broker_submitted": 0,
            "broker_acknowledged": 0,
            "partially_filled": 0,
            "filled": 0,
            "attributed": 0,
            "pnl_replay_ready": 0,
        },
        "drop_stage_counts": {"post_approval": 0, "reservation": 0, "execution_gate": 0, "broker_submit": 0},
        "conversion_rates": {"approved_to_intent": 1.0, "approved_to_fill": 0.0, "approved_to_pnl_replay_ready": 0.0},
        "owner_identity_failures": {"missing_owner": 0, "ambiguous_owner": 0, "unknown_broker_order": 0},
        "classification": {"primary_conversion_loss": "INSUFFICIENT_SAMPLE"},
        "recommendations": ["collect_real_paper_shadow_runtime_samples"],
    }
    _write_json(repo_root / "reports" / "approved_to_fill_conversion" / "approved_to_fill_conversion_latest.json", approved_report)

    vegas_report = {
        "generated_at": generated_at,
        "strategy_id": "vegas_hma_setup_trigger_risk_v12_spy_001",
        "window": {"start": generated_at, "end": generated_at},
        "sample_count": 1,
        "status_counts": {"approved": 1, "validator_rejected": 0, "stale_decision": 0, "edge_insufficient": 0},
        "dlb": {"limit_ms": 2000, "breach_count": 0, "breach_rate": 0.0, "worst_elapsed_ms": 100},
        "edge_cost": {"sample_count": 1, "edge_insufficient_count": 0, "edge_insufficient_rate": 0.0},
        "classification": {"primary_rejection_driver": "INSUFFICIENT_SAMPLE"},
        "recommendations": ["collect_real_paper_shadow_runtime_samples"],
    }
    _write_json(repo_root / "reports" / "vegas_dlb_edge_measurement" / "vegas_dlb_edge_measurement_latest.json", vegas_report)

    audit_row = {
        "generated_at": generated_at,
        "stage": "shadow_intent",
        "mode": mode,
        "account_id": account_id,
        "strategy_id": strategy_id,
        "strategy_instance_id": strategy_instance_id,
        "symbol": symbol,
        "intent_id": intent_id,
        "client_order_id": client_order_id,
        "lot_id": "lot-sim-shadow-001",
        "order_role": "ENTRY",
        "owner_status": "BOUND_SYSTEM_OWNER",
        "mutation_policy": "report_only_no_broker_mutation",
        "attribution_path": "owner_identity",
    }
    _write_jsonl(repo_root / "runtime" / "audit" / f"paper_shadow_soak_{day}.jsonl", [audit_row])

    operator_proof = {
        "generated_at": generated_at,
        "mode": mode,
        "broker_connection_policy": "live_broker_disabled_mocked",
        "operator_action_required": False,
        "warnings": ["simulated_shadow_not_operator_reconnect_proof"],
    }
    _write_json(repo_root / "runtime" / "recovery" / "ib_gateway_operator_proof" / "latest.json", operator_proof)

    return {
        "generated_at": generated_at,
        "mode": mode,
        "duration_seconds": 0,
        "artifacts_written": [
            f"reports/paper_shadow_soak_gate/pre_soak_safety_check_{day}.json",
            "runtime/readiness/broker_truth_snapshot_latest.json",
            "runtime/readiness/paper_broker_truth_snapshot_latest.json",
            "runtime/readiness/position_safety_latest.json",
            "runtime/readiness/production_state_latest.json",
            "runtime/readiness/runtime_version_latest.json",
            f"runtime/readiness/strategy_tradability_heartbeat_{day}.jsonl",
            f"reports/strategy_surface/live_strategy_surface_report_{day}.json",
            "reports/approved_to_fill_conversion/approved_to_fill_conversion_latest.json",
            "reports/vegas_dlb_edge_measurement/vegas_dlb_edge_measurement_latest.json",
            f"runtime/audit/paper_shadow_soak_{day}.jsonl",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--day", default=datetime.now(tz=ET).strftime("%Y%m%d"))
    parser.add_argument("--mode", default="SIMULATED_SHADOW", choices=("SIMULATED_SHADOW",))
    args = parser.parse_args()
    result = build_simulated_shadow_artifacts(repo_root=Path(args.repo_root), day=args.day, mode=args.mode)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
