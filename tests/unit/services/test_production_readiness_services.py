from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.data_availability import (
    ET,
    DataAvailabilityService,
    configure_data_availability_service,
    get_data_availability_service,
)
from services.market.market_data_service import MarketDataService
from services.production.eod_supervisor import PlatformEODSupervisor, session_close_policies_from_config
from services.production.position_safety_supervisor import (
    PositionSafetySupervisor,
    validate_position_safety_broker_truth_dependency,
)
from services.production.production_state_emitter import ProductionStateEmitter
from services.production.recovery_incident_service import RecoveryIncidentService
from services.production.session_end_supervisor import SessionEndSupervisor
from services.production.ib_restart_drill_artifact import build_ib_restart_drill_artifact
from services.production.readiness_gate import ProductionReadinessGate
from services.production.recovery_readiness import RecoveryReadinessService, get_recovery_readiness_service
from services.production.strategy_data_contract import (
    build_strategy_data_contracts,
    validate_strategy_production_manifests,
)
from services.production.strategy_tradability import (
    build_strategy_tradability_snapshot,
    read_strategy_tradability_heartbeats,
    write_strategy_tradability_heartbeat,
)
from tool.ops.run_runtime_operations_watchdog import _build_production_state


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_production_state_splits_entry_no_go_from_safety_go() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "day": "20260511",
            "recommendation": "HOLD",
            "failures": [
                {
                    "issue_key": "contract_selection:selected_contract_identity_invalid",
                    "service": "CONTRACT_SELECTION",
                    "error_type": "contract_selection_no_go",
                    "detail": "selected_contract_identity_invalid",
                }
            ],
            "contract_selection": {"go_no_go": "no_go", "blockers": ["selected_contract_identity_invalid"]},
        }
    )

    assert state["go_no_go"] == "go"
    assert state["safety_go_no_go"] == "go"
    assert state["entry_go_no_go"] == "no_go"
    assert state["safety_failure_count"] == 0
    assert state["entry_failure_count"] == 1


def test_production_state_exposes_top_level_runtime_identity() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-14T10:00:00-04:00",
            "day": "20260514",
            "recommendation": "PASS",
            "failures": [],
            "runtime_version": {
                "process_pid": 12345,
                "process_start_time": "2026-05-14T09:29:00-04:00",
                "git_head": "abc123",
                "config_hash": "cfg456",
                "runtime_id": "runtime-1",
            },
        }
    )

    assert state["runtime_pid"] == 12345
    assert state["runtime_process_start_time"] == "2026-05-14T09:29:00-04:00"
    assert state["runtime_git_sha"] == "abc123"
    assert state["runtime_config_hash"] == "cfg456"
    assert state["runtime_lifecycle_generation"] == "runtime-1"
    assert state["artifact_source_component"] == "runtime_operations_watchdog"


def test_production_state_keeps_broker_truth_failure_hard_no_go() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "day": "20260511",
            "recommendation": "HOLD",
            "failures": [
                {
                    "issue_key": "broker_truth_snapshot:broker_truth_live_snapshot_missing",
                    "service": "BROKER_TRUTH",
                    "error_type": "broker_truth_snapshot_no_go",
                    "detail": "broker_truth_live_snapshot_missing",
                }
            ],
            "broker_truth_snapshot": {"go_no_go": "no_go", "blockers": ["broker_truth_live_snapshot_missing"]},
        }
    )

    assert state["go_no_go"] == "no_go"
    assert state["safety_go_no_go"] == "no_go"
    assert state["entry_go_no_go"] == "no_go"
    assert state["safety_failure_count"] == 1
    assert state["entry_failure_count"] == 0


def test_production_state_represents_eod_manual_lock_as_scoped_entry_block() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-12T16:05:00-04:00",
            "day": "20260512",
            "recommendation": "HOLD",
            "failures": [
                {
                    "issue_key": "platform_eod_no_go",
                    "service": "PLATFORM_EOD",
                    "error_type": "platform_eod_no_go",
                    "detail": "MANUAL_REQUIRED",
                    "manual_lock_ids": ["platform_eod_manual_lock:2026-05-12:strategy_a_001:NVDA"],
                },
                {
                    "issue_key": "manual_intervention_lock_active",
                    "service": "MANUAL_LOCK",
                    "error_type": "manual_intervention_lock_active",
                    "detail": "active_db_manual_intervention_lock",
                    "active_count": 1,
                    "locks": [
                        {
                            "strategy_id": "strategy_a",
                            "symbol": "NVDA",
                            "reason": "platform_eod_manual_required",
                            "context": {"account_id": "DU123", "strategy_instance_id": "strategy_a_001"},
                        }
                    ],
                },
            ],
            "strategy_tradability": {
                "go_no_go": "no_go",
                "tradable_entry_ready_count": 1,
                "close_only_allowed_count": 1,
            },
        }
    )

    assert state["go_no_go"] == "go"
    assert state["safety_go_no_go"] == "go"
    assert state["global_halt"] is False
    assert state["entry_go_no_go"] == "partial"
    assert state["safety_failure_count"] == 0
    assert state["entry_failure_count"] == 2
    assert not any(item["scope_type"] == "GLOBAL" for item in state["blocked_scopes"])
    assert any(
        item["scope_type"] == "STRATEGY_INSTANCE"
        and item["strategy_instance_id"] == "strategy_a_001"
        and item["symbol"] == "NVDA"
        for item in state["blocked_scopes"]
    )
    assert not any(
        item["scope_type"] == "SESSION"
        and "platform_eod_manual_lock:2026-05-12:strategy_a_001:NVDA" in item["blockers"]
        for item in state["blocked_scopes"]
    )


def _partial_entry_scoped_report() -> dict:
    return {
        "generated_at": "2026-05-12T16:05:00-04:00",
        "day": "20260512",
        "recommendation": "HOLD",
        "failures": [
            {
                "issue_key": "strategy_tradability:strategy_a:session_closed",
                "service": "STRATEGY_TRADABILITY",
                "error_type": "strategy_tradability_no_go",
                "detail": "session_closed",
            }
        ],
        "strategy_tradability": {
            "go_no_go": "no_go",
            "tradable_entry_ready_count": 1,
            "close_only_allowed_count": 1,
            "records": [
                {
                    "strategy_id": "strategy_a",
                    "tradable_entry_ready": False,
                    "close_only_allowed": True,
                    "blockers": ["session_closed"],
                    "details": {
                        "account_id": "DU123",
                        "strategy_instance_id": "strategy_a_001",
                        "symbol": "NVDA",
                        "session": "POST",
                    },
                },
                {
                    "strategy_id": "strategy_b",
                    "tradable_entry_ready": True,
                    "close_only_allowed": False,
                    "blockers": [],
                    "details": {
                        "account_id": "DU123",
                        "strategy_instance_id": "strategy_b_001",
                        "symbol": "MSFT",
                        "session": "POST",
                    },
                },
            ],
        },
    }


def test_production_state_go_with_entry_partial_scoped_blocks() -> None:
    state = _build_production_state(_partial_entry_scoped_report())

    assert state["go_no_go"] == "go"
    assert state["safety_go_no_go"] == "go"
    assert state["entry_go_no_go"] == "partial"
    assert state["global_halt"] is False
    assert state["tradable_entry_ready_count"] == 1
    assert state["close_only_allowed_count"] == 1


def test_production_state_partial_not_interpreted_as_global_go_for_blocked_strategy() -> None:
    state = _build_production_state(_partial_entry_scoped_report())

    assert state["production_state_authorizes_execution"] is False
    assert any(
        item["strategy_instance_id"] == "strategy_a_001"
        and item["flow_type"] == "ENTRY"
        and item["blockers"] == ["session_closed"]
        for item in state["blocked_scopes"]
    )


def test_production_state_partial_not_interpreted_as_global_no_go_for_allowed_strategy() -> None:
    state = _build_production_state(_partial_entry_scoped_report())

    assert state["go_no_go"] == "go"
    assert state["global_halt"] is False
    assert not any(item["strategy_instance_id"] == "strategy_b_001" for item in state["blocked_scopes"])


def test_production_state_blocked_scopes_include_account_strategy_instance_symbol_flow() -> None:
    state = _build_production_state(_partial_entry_scoped_report())

    blocked = state["blocked_scopes"][0]
    assert blocked["account_id"] == "DU123"
    assert blocked["strategy_instance_id"] == "strategy_a_001"
    assert blocked["symbol"] == "NVDA"
    assert blocked["flow_type"] == "ENTRY"
    assert blocked["authority"] == "TradabilityService"


def test_production_state_flow_overrides_include_close_only_strategy_flatten() -> None:
    state = _build_production_state(_partial_entry_scoped_report())

    assert any(
        item["flow_type"] == "STRATEGY_FLATTEN"
        and item["allowed"] is True
        and item["scope"]["strategy_instance_id"] == "strategy_a_001"
        for item in state["flow_overrides"]
    )


def test_production_state_global_halt_still_blocks_all_entry() -> None:
    state = _build_production_state(
        {
            **_partial_entry_scoped_report(),
            "failures": [
                {
                    "issue_key": "broker_truth_snapshot:broker_truth_live_snapshot_missing",
                    "service": "BROKER_TRUTH",
                    "error_type": "broker_truth_snapshot_no_go",
                    "detail": "broker_truth_live_snapshot_missing",
                }
            ],
            "broker_truth_snapshot": {
                "go_no_go": "no_go",
                "blockers": ["broker_truth_live_snapshot_missing"],
            },
        }
    )

    assert state["go_no_go"] == "no_go"
    assert state["safety_go_no_go"] == "no_go"
    assert state["entry_go_no_go"] == "no_go"
    assert state["global_halt"] is True


def test_production_state_scopes_option_trigger_ledger_to_required_strategies() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-13T10:00:00-04:00",
            "day": "20260513",
            "recommendation": "HOLD",
            "failures": [
                {
                    "issue_key": "option_trigger_active_without_terminal_verdict",
                    "service": "OPTION_TRIGGER_LEDGER",
                    "error_type": "option_trigger_active_without_terminal_verdict",
                    "detail": "recent_option_trigger_lacks_selected_rejected_or_expired_verdict",
                    "required_strategy_ids": [
                        "qqq_intraday_option_v1_001",
                        "spy_1dte_ma10_reject_0324_001",
                    ],
                }
            ],
            "strategy_tradability": {
                "go_no_go": "partial",
                "tradable_entry_ready_count": 1,
                "close_only_allowed_count": 0,
            },
        }
    )

    blocked = {
        item["strategy_instance_id"]: item
        for item in state["blocked_scopes"]
        if item["blockers"] == ["option_trigger_active_without_terminal_verdict"]
    }
    assert state["go_no_go"] == "go"
    assert state["entry_go_no_go"] == "partial"
    assert set(blocked) == {"qqq_intraday_option_v1_001", "spy_1dte_ma10_reject_0324_001"}
    assert all(item["scope_type"] == "STRATEGY_INSTANCE" for item in blocked.values())
    assert not any(
        item["scope_type"] == "EXACT"
        and item["blockers"] == ["option_trigger_active_without_terminal_verdict:recent_option_trigger_lacks_selected_rejected_or_expired_verdict"]
        for item in state["blocked_scopes"]
    )


def test_production_state_scopes_strategy_tradability_failure_without_bucket_widening() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-13T10:00:00-04:00",
            "day": "20260513",
            "recommendation": "HOLD",
            "failures": [
                {
                    "issue_key": "strategy_tradability:nvda_minute_flip_paper_001:order_lifecycle_pending",
                    "service": "STRATEGY_TRADABILITY",
                    "error_type": "strategy_tradability_no_go",
                    "detail": "nvda_minute_flip_paper_001:order_lifecycle_pending",
                }
            ],
            "strategy_tradability": {
                "go_no_go": "partial",
                "tradable_entry_ready_count": 1,
                "close_only_allowed_count": 0,
            },
        }
    )

    blocked = [
        item
        for item in state["blocked_scopes"]
        if item["blockers"] == ["order_lifecycle_pending"]
    ]
    assert len(blocked) == 1
    assert blocked[0]["scope_type"] == "STRATEGY_INSTANCE"
    assert blocked[0]["strategy_instance_id"] == "nvda_minute_flip_paper_001"
    assert not any(
        item["blockers"]
        == [
            "strategy_tradability:nvda_minute_flip_paper_001:order_lifecycle_pending:nvda_minute_flip_paper_001:order_lifecycle_pending"
        ]
        for item in state["blocked_scopes"]
    )


def test_production_state_counts_idle_separately_from_entry_failures() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "day": "20260511",
            "recommendation": "HOLD",
            "failures": [],
            "strategy_tradability": {
                "go_no_go": "go",
                "idle_count": 2,
                "stale_heartbeat_count": 0,
                "tradable_entry_ready_count": 1,
                "close_only_allowed_count": 1,
            },
        }
    )

    assert state["entry_failure_count"] == 0
    assert state["idle_count"] == 2
    assert state["stale_heartbeat_count"] == 0
    assert state["tradable_entry_ready_count"] == 1
    assert state["close_only_allowed_count"] == 1


def test_production_state_counts_stale_heartbeat_separately() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "day": "20260511",
            "recommendation": "HOLD",
            "failures": [],
            "strategy_tradability": {
                "go_no_go": "no_go",
                "idle_count": 0,
                "stale_heartbeat_count": 3,
                "tradable_entry_ready_count": 0,
                "close_only_allowed_count": 0,
            },
        }
    )

    assert state["entry_failure_count"] == 0
    assert state["stale_heartbeat_count"] == 3


def test_production_state_separates_safety_entry_idle_stale_buckets() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "day": "20260511",
            "recommendation": "HOLD",
            "failures": [
                {
                    "issue_key": "broker_truth_live_snapshot_missing",
                    "service": "BROKER_TRUTH",
                    "detail": "broker_truth_live_snapshot_missing",
                },
                {
                    "issue_key": "strategy_tradability:qqq_intraday_001:stream_invalid",
                    "service": "STRATEGY_TRADABILITY",
                    "detail": "qqq_intraday_001:stream_invalid",
                },
                {
                    "issue_key": "session_end_latest_stale",
                    "service": "SESSION_END",
                    "detail": "artifact_exceeded_max_age_sec",
                },
            ],
            "strategy_tradability": {
                "idle_count": 1,
                "stale_heartbeat_count": 1,
                "tradable_entry_ready_count": 0,
                "close_only_allowed_count": 1,
                "records": [
                    {
                        "strategy_id": "idle_strategy",
                        "idle_reason": "offsession",
                        "blockers": [],
                    },
                    {
                        "strategy_id": "stale_strategy",
                        "blockers": ["fresh_tradability_heartbeat_stale"],
                    },
                ],
            },
        }
    )

    assert state["summary"]["failure_count"] == 4
    assert state["summary"]["safety_failure_count"] == 1
    assert state["summary"]["entry_failure_count"] == 1
    assert state["summary"]["artifact_stale_count"] == 2
    assert state["buckets"]["idle_reasons"] == ["idle_strategy:offsession"]
    assert any("broker_truth_live_snapshot_missing" in item for item in state["buckets"]["safety_blockers"])
    assert any("stream_invalid" in item for item in state["buckets"]["entry_blockers"])
    assert any("session_end_latest_stale" in item for item in state["buckets"]["artifact_stale_blockers"])
    assert any("stale_strategy:fresh_tradability_heartbeat_stale" in item for item in state["buckets"]["artifact_stale_blockers"])


def test_production_state_failure_count_is_explainable_sum() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "failures": [
                {"issue_key": "broker_truth_lookup_failed", "service": "BROKER_TRUTH", "detail": "lookup_failed"},
                {"issue_key": "runtime_version_loaded_code_stale", "service": "RUNTIME_VERSION", "detail": "stale"},
                {"issue_key": "strategy_tradability:s1:stream_invalid", "service": "STRATEGY_TRADABILITY", "detail": "s1:stream_invalid"},
            ],
        }
    )

    summary = state["summary"]
    assert summary["failure_count"] == (
        summary["safety_failure_count"]
        + summary["entry_failure_count"]
        + summary["artifact_stale_count"]
        + summary["runtime_failure_count"]
    )
    assert state["go_no_go"] == "no_go"


def test_production_state_does_not_count_idle_or_offsession_as_failure() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "strategy_tradability:idle_strategy:idle_not_in_entry_window",
                    "service": "STRATEGY_TRADABILITY",
                    "detail": "idle_strategy:idle_not_in_entry_window",
                },
                {
                    "issue_key": "strategy_tradability:offsession_strategy:offsession",
                    "service": "STRATEGY_TRADABILITY",
                    "detail": "offsession_strategy:offsession",
                },
            ],
            "strategy_tradability": {
                "idle_count": 2,
                "records": [
                    {"strategy_id": "idle_strategy", "idle_reason": "not_in_entry_window", "blockers": ["idle_not_in_entry_window"]},
                    {"strategy_id": "offsession_strategy", "idle_reason": "offsession", "blockers": ["offsession"]},
                ],
            },
        }
    )

    assert state["failure_count"] == 0
    assert state["entry_failure_count"] == 0
    assert state["idle_count"] == 2
    assert state["buckets"]["entry_blockers"] == []


def test_production_state_dedupes_global_risk_blocker_from_strategy_blockers() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "broker_truth_live_snapshot_missing",
                    "service": "BROKER_TRUTH",
                    "detail": "broker_truth_live_snapshot_missing",
                },
                {
                    "issue_key": "strategy_tradability:s1:broker_truth_missing",
                    "service": "STRATEGY_TRADABILITY",
                    "detail": "s1:broker_truth_missing",
                },
            ],
        }
    )

    assert state["safety_failure_count"] == 1
    assert state["entry_failure_count"] == 0
    assert state["failure_count"] == 1


def test_production_state_runtime_version_stale_bucket() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "runtime_version_loaded_code_stale",
                    "service": "RUNTIME_VERSION",
                    "detail": "run_system_process_started_before_modified_critical_modules",
                }
            ],
            "runtime_version": {"loaded_code_current": False},
        }
    )

    assert state["go_no_go"] == "no_go"
    assert state["runtime_failure_count"] == 1
    assert state["buckets"]["runtime_blockers"][0].startswith("runtime_version_loaded_code_stale")
    assert state["readiness"]["runtime_version"] == "no_go"


def test_runtime_stale_goes_to_runtime_bucket_not_entry_failure() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-12T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "runtime_version_loaded_code_stale",
                    "service": "RUNTIME_VERSION",
                    "detail": "run_system_process_started_before_modified_critical_modules",
                }
            ],
            "runtime_version": {
                "loaded_code_current": False,
                "freshness": {
                    "status": "STALE_CRITICAL_MODULE",
                    "restart_required": True,
                    "critical_modules_stale_count": 1,
                },
            },
        }
    )

    assert state["go_no_go"] == "no_go"
    assert state["runtime_failure_count"] == 1
    assert state["entry_failure_count"] == 0
    assert state["summary"]["failure_count"] == 1
    assert state["readiness_details"]["runtime_version"]["restart_required"] is True


def test_runtime_fresh_clears_runtime_blocker() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-12T10:00:00-04:00",
            "failures": [],
            "runtime_version": {
                "loaded_code_current": True,
                "freshness": {
                    "status": "CURRENT",
                    "restart_required": False,
                    "critical_modules_stale_count": 0,
                },
            },
        }
    )

    assert state["go_no_go"] == "go"
    assert state["runtime_failure_count"] == 0
    assert state["buckets"]["runtime_blockers"] == []
    assert state["readiness_details"]["runtime_version"]["status"] == "go"


def test_production_state_readiness_includes_runtime_version_status() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-12T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "runtime_version_loaded_code_stale",
                    "service": "RUNTIME_VERSION",
                    "detail": "run_system_process_started_before_modified_critical_modules",
                }
            ],
            "runtime_version": {
                "process_pid": 123,
                "process_start_time": "2026-05-12T09:30:00-04:00",
                "runtime_id": "123:2026-05-12T09:30:00-04:00",
                "restart_id": "123:2026-05-12T09:30:00-04:00",
                "loaded_code_current": False,
                "stale_modules": [{"path": "services/exec/safety_kernel.py"}],
                "freshness": {
                    "status": "STALE_CRITICAL_MODULE",
                    "restart_required": True,
                    "critical_modules_stale_count": 1,
                },
            },
        }
    )

    runtime = state["readiness_details"]["runtime_version"]
    assert runtime["status"] == "no_go"
    assert runtime["freshness_status"] == "STALE_CRITICAL_MODULE"
    assert runtime["loaded_code_current"] is False
    assert runtime["stale_modules_count"] == 1
    assert runtime["process_pid"] == 123


def test_production_state_broker_truth_and_position_safety_blockers_preserve_reason() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "broker_truth_not_broker_backed",
                    "service": "BROKER_TRUTH",
                    "detail": "db_projection_only",
                },
                {
                    "issue_key": "position_safety_lookup_failed",
                    "service": "POSITION_SAFETY",
                    "detail": "broker_truth_lookup_failed",
                },
            ],
            "broker_truth_snapshot": {"go_no_go": "no_go"},
            "position_safety": {"go_no_go": "no_go"},
        }
    )

    assert state["safety_failure_count"] == 2
    assert any("db_projection_only" in item for item in state["buckets"]["safety_blockers"])
    assert any("broker_truth_lookup_failed" in item for item in state["buckets"]["safety_blockers"])
    assert state["readiness"]["broker_truth"] == "no_go"
    assert state["readiness"]["position_safety"] == "no_go"


def test_production_state_strategy_surface_summary_counts_tiers() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "strategy_surface": {
                "strategies": [
                    {"strategy_id": "canary", "permission_tier": "CANARY_ONLY", "path_type": "OPTIONS_KERNEL_PURE_PLUGIN"},
                    {"strategy_id": "shadow", "permission_tier": "SHADOW_ONLY", "path_type": "DIRECT_STRATEGY_EXECUTION_PATH"},
                    {
                        "strategy_id": "disabled",
                        "permission_tier": "DISABLED_UNTIL_CONTRACT_PARITY",
                        "path_type": "LEGACY_STRATEGY_EXECUTION_PATH",
                    },
                ]
            },
            "strategy_tradability": {
                "tradable_entry_ready_count": 1,
                "close_only_allowed_count": 0,
            },
        }
    )

    assert state["strategy_surface_summary"] == {
        "production_eligible_count": 0,
        "canary_only_count": 1,
        "shadow_only_count": 1,
        "disabled_until_contract_parity_count": 1,
        "unknown_path_count": 0,
    }
    assert state["summary"]["strategy_surface"]["production_eligible_count"] == 0
    assert state["tradable_entry_ready_count"] == 1


def test_production_state_excludes_shadow_from_production_entry_ready_count() -> None:
    state = _build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "strategy_surface": {
                "strategy_surface_summary": {
                    "production_eligible_count": 0,
                    "canary_only_count": 0,
                    "shadow_only_count": 2,
                    "disabled_until_contract_parity_count": 1,
                    "unknown_path_count": 0,
                }
            },
            "strategy_tradability": {
                "tradable_entry_ready_count": 2,
                "close_only_allowed_count": 1,
            },
        }
    )

    assert state["strategy_surface_summary"]["production_eligible_count"] == 0
    assert state["summary"]["tradable_entry_ready_count"] == 2
    assert state["summary"]["strategy_surface"]["shadow_only_count"] == 2


def test_ib_restart_drill_blocks_day_flapping(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IB_RESTART_DRILL_DAY_MAX_DISCONNECT_EVENTS", "2")
    monkeypatch.setenv("IB_RESTART_DRILL_DAY_MAX_RECONNECT_EVENTS", "4")
    monkeypatch.setenv("IB_RESTART_DRILL_DAY_MAX_RESUMED_EVENTS", "4")
    day_dir = tmp_path / "runtime/audit/2026-05-05"
    system_rows = []
    for idx in range(3):
        system_rows.append(
            {
                "ts": f"2026-05-05T10:0{idx}:00-04:00",
                "component": "market_data_service",
                "stage": "ib_disconnected",
                "status": "disconnected",
            }
        )
        system_rows.append(
            {
                "ts": f"2026-05-05T10:0{idx}:10-04:00",
                "component": "market_data_service",
                "stage": "ib_connected",
                "status": "connected",
            }
        )
        system_rows.append(
            {
                "ts": f"2026-05-05T10:0{idx}:20-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "recovering",
                "payload": {"event_type": "system_recovering"},
            }
        )
        system_rows.append(
            {
                "ts": f"2026-05-05T10:0{idx}:30-04:00",
                "component": "system_health_controller",
                "stage": "health_transition",
                "status": "running",
                "payload": {
                    "event_type": "system_resumed",
                    "metadata": {
                        "platform_recovery_ready": True,
                        "recovery_readiness": {"ready": True},
                        "ghosts": 0,
                        "unresolved_orphans": 0,
                        "mismatches": 0,
                        "position_mismatches": 0,
                        "active_quarantine_count": 0,
                    },
                },
            }
        )
    _append_jsonl(day_dir / "system-10.jsonl", system_rows)
    _append_jsonl(
        day_dir / "recon-10.jsonl",
        [
            {
                "ts": "2026-05-05T10:02:25-04:00",
                "component": "runtime_reconciler",
                "stage": "position_audit_snapshot",
                "payload": {
                    "ghosts": 0,
                    "orphans_unresolved": 0,
                    "mismatches": 0,
                    "position_mismatches": 0,
                    "active_quarantine_count": 0,
                },
            }
        ],
    )

    artifact = build_ib_restart_drill_artifact(repo_root=tmp_path, day="2026-05-05")

    assert artifact["go_no_go"] == "no_go"
    assert "ib_day_disconnect_flapping_threshold_exceeded" in artifact["blockers"]


@pytest.mark.asyncio
async def test_production_state_emitter_writes_in_process_snapshot(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog

    def fake_report(*, repo_root):
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "PASS",
            "failures": [],
            "system_health": {"state": "running"},
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": True},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)
    class _Controller:
        def __init__(self) -> None:
            self.calls = []

        def emit_heartbeat(self, **kwargs) -> None:
            self.calls.append(kwargs)

    controller = _Controller()
    emitter = ProductionStateEmitter(
        repo_root=tmp_path,
        enabled=True,
        interval_sec=30,
        system_health_controller=controller,
    )

    state = await emitter.run_once()

    path = tmp_path / "runtime/readiness/production_state/production_state_latest.json"
    assert path.exists()
    assert state["go_no_go"] == "go"
    assert state["failure_count"] == 0
    assert controller.calls[-1]["reason"] == "production_state_emitter_heartbeat"


@pytest.mark.asyncio
async def test_production_state_emitter_projects_through_runtime_lifecycle(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog
    from services.runtime.lifecycle_controller import RuntimeLifecycleController

    def fake_report(*, repo_root):
        return {
            "generated_at": "2026-05-14T10:00:00-04:00",
            "day": "20260514",
            "recommendation": "PASS",
            "failures": [],
            "system_health": {"state": "running"},
            "runtime_version": {"process_pid": 123, "runtime_id": "runtime-1"},
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": True},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)

    class _Controller:
        state = "running"

        def __init__(self) -> None:
            self.heartbeats = []

        def emit_heartbeat(self, **kwargs) -> None:
            self.heartbeats.append(kwargs)

    health = _Controller()
    lifecycle = RuntimeLifecycleController(runtime_identity={"runtime_pid": 123})
    emitter = ProductionStateEmitter(
        repo_root=tmp_path,
        enabled=True,
        interval_sec=30,
        system_health_controller=health,
        runtime_lifecycle_controller=lifecycle,
    )

    state = await emitter.run_once()

    assert state["go_no_go"] == "go"
    assert lifecycle.snapshot().state == "RUNNING"
    assert health.heartbeats[-1]["reason"] == "runtime_lifecycle_projection"
    assert health.heartbeats[-1]["metadata"]["runtime_lifecycle_state"] == "RUNNING"


@pytest.mark.asyncio
async def test_production_state_emitter_refreshes_position_safety_before_watchdog(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog

    events: list[str] = []

    async def refresh_position_safety() -> None:
        events.append("refresh")

    def fake_report(*, repo_root):
        events.append("watchdog")
        return {
            "generated_at": "2026-05-14T10:00:00-04:00",
            "day": "20260514",
            "recommendation": "PASS",
            "failures": [],
            "system_health": {"state": "running"},
            "runtime_version": {"process_pid": 123, "runtime_id": "runtime-1"},
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": True},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)

    emitter = ProductionStateEmitter(
        repo_root=tmp_path,
        enabled=True,
        interval_sec=30,
        readiness_refreshers=[("position_safety", refresh_position_safety)],
    )

    state = await emitter.run_once()

    assert state["go_no_go"] == "go"
    assert events == ["refresh", "watchdog"]


@pytest.mark.asyncio
async def test_production_state_emitter_builds_current_process_ib_drill(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog
    import services.production.ib_restart_drill_artifact as drill_module

    calls = {"report": 0, "drill": 0}

    def fake_report(*, repo_root):
        calls["report"] += 1
        failures = (
            [{"issue_key": "ib_restart_drill_artifact_before_process_start"}]
            if calls["report"] == 1
            else []
        )
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "HOLD" if failures else "PASS",
            "failures": failures,
            "system_health": {"state": "running"},
            "runtime_version": {"process_start_time": "2026-05-04T09:30:00-04:00"},
            "ib_restart_drill": {
                "required": True,
                "artifact_present": True,
                "artifact_before_process_start": calls["report"] == 1,
                "go_no_go": "go",
                "process_start_time": "2026-05-04T09:30:00-04:00",
            },
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": True},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    def fake_build(**kwargs):
        calls["drill"] += 1
        assert kwargs["start_ts"] == "2026-05-04T09:30:00-04:00"
        return {"go_no_go": "go", "blockers": []}

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)
    monkeypatch.setattr(drill_module, "build_ib_restart_drill_artifact", fake_build)

    emitter = ProductionStateEmitter(repo_root=tmp_path, enabled=True, interval_sec=30)

    state = await emitter.run_once()

    assert calls == {"report": 2, "drill": 1}
    assert state["go_no_go"] == "go"


@pytest.mark.asyncio
async def test_production_state_emitter_rebuilds_existing_bad_ib_drill_even_when_not_required(
    tmp_path: Path, monkeypatch
) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog
    import services.production.ib_restart_drill_artifact as drill_module

    calls = {"report": 0, "drill": 0}

    def fake_report(*, repo_root):
        calls["report"] += 1
        failures = [{"issue_key": "ib_restart_drill_no_go"}] if calls["report"] == 1 else []
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "HOLD" if failures else "PASS",
            "failures": failures,
            "system_health": {"state": "running"},
            "runtime_version": {"process_start_time": "2026-05-04T09:30:00-04:00"},
            "ib_restart_drill": {
                "required": False,
                "artifact_present": True,
                "artifact_before_process_start": False,
                "go_no_go": "no_go",
                "process_start_time": "2026-05-04T09:30:00-04:00",
            },
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": True},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    def fake_build(**kwargs):
        calls["drill"] += 1
        assert kwargs["start_ts"] == "2026-05-04T09:30:00-04:00"
        return {"go_no_go": "no_go", "blockers": ["ib_disconnect_event_missing"]}

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)
    monkeypatch.setattr(drill_module, "build_ib_restart_drill_artifact", fake_build)

    emitter = ProductionStateEmitter(repo_root=tmp_path, enabled=True, interval_sec=30)

    state = await emitter.run_once()

    assert calls == {"report": 2, "drill": 1}
    assert state["go_no_go"] == "go"


@pytest.mark.asyncio
async def test_production_state_emitter_hard_exits_after_core_lifecycle_failures(tmp_path: Path, monkeypatch) -> None:
    import os
    import tool.ops.run_runtime_operations_watchdog as watchdog

    def fake_report(*, repo_root):
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "HOLD",
            "failures": [{"issue_key": "system_health_transition_stale", "detail": "stale_transition"}],
            "system_health": {"state": "degraded"},
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": True},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    exits = []
    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    monkeypatch.setenv("PRODUCTION_STATE_EXIT_ON_CORE_STOPPED", "1")
    monkeypatch.setenv("PRODUCTION_STATE_EXIT_AFTER_CORE_STOPPED_COUNT", "2")

    emitter = ProductionStateEmitter(repo_root=tmp_path, enabled=True, interval_sec=30)
    await emitter.run_once()
    assert exits == []
    await emitter.run_once()
    assert exits == [78]


@pytest.mark.asyncio
async def test_production_state_emitter_publishes_degraded_on_no_go(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog

    def fake_report(*, repo_root):
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "HOLD",
            "failures": [{"issue_key": "position_safety_no_go"}],
            "system_health": {"state": "running"},
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": True},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)

    class _Controller:
        state = "running"
        last_event = {}

        def __init__(self) -> None:
            self.published = []

        async def publish(self, **kwargs) -> None:
            self.published.append(kwargs)

    controller = _Controller()
    emitter = ProductionStateEmitter(repo_root=tmp_path, enabled=True, interval_sec=30, system_health_controller=controller)

    state = await emitter.run_once()

    assert state["go_no_go"] == "no_go"
    assert controller.published[-1]["event_type"] == "system_degraded"
    assert controller.published[-1]["reason"] == "production_state_no_go"
    assert controller.published[-1]["metadata"]["production_state_failure_count"] == 1


@pytest.mark.asyncio
async def test_production_state_emitter_resumes_on_pause_derived_only_no_go(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog

    def fake_report(*, repo_root):
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "HOLD",
            "failures": [{"issue_key": "strategy_tradability:paper_001:system_paused"}],
            "system_health": {"state": "degraded"},
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": False},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)

    class _Controller:
        state = "degraded"
        last_event = {}

        def __init__(self) -> None:
            self.published = []
            self.heartbeats = []

        async def publish(self, **kwargs) -> None:
            self.published.append(kwargs)

        def emit_heartbeat(self, **kwargs) -> None:
            self.heartbeats.append(kwargs)

    controller = _Controller()
    emitter = ProductionStateEmitter(repo_root=tmp_path, enabled=True, interval_sec=30, system_health_controller=controller)

    state = await emitter.run_once()

    assert state["go_no_go"] == "go"
    assert state["safety_go_no_go"] == "go"
    assert state["entry_go_no_go"] == "go"
    assert controller.heartbeats == []
    assert controller.published[-1]["event_type"] == "system_resumed"
    assert controller.published[-1]["reason"] == "production_state_go"


@pytest.mark.asyncio
async def test_production_state_emitter_does_not_degrade_on_ib_drill_no_go(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog

    def fake_report(*, repo_root):
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "HOLD",
            "failures": [{"issue_key": "ib_restart_drill_no_go"}],
            "system_health": {"state": "recovering"},
            "ib_restart_drill": {
                "required": False,
                "artifact_present": True,
                "artifact_before_process_start": False,
                "go_no_go": "no_go",
            },
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": False},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)

    class _Controller:
        state = "recovering"
        last_event = {}

        def __init__(self) -> None:
            self.published = []
            self.heartbeats = []

        async def publish(self, **kwargs) -> None:
            self.published.append(kwargs)

        def emit_heartbeat(self, **kwargs) -> None:
            self.heartbeats.append(kwargs)

    controller = _Controller()
    emitter = ProductionStateEmitter(repo_root=tmp_path, enabled=True, interval_sec=30, system_health_controller=controller)

    state = await emitter.run_once()

    assert state["go_no_go"] == "go"
    assert state["safety_go_no_go"] == "go"
    assert state["entry_go_no_go"] == "no_go"
    assert controller.published[-1]["event_type"] == "system_resumed"
    assert controller.published[-1]["reason"] == "production_state_go"
    assert controller.heartbeats == []


@pytest.mark.asyncio
async def test_production_state_emitter_does_not_degrade_on_strategy_data_gate(tmp_path: Path, monkeypatch) -> None:
    import tool.ops.run_runtime_operations_watchdog as watchdog

    def fake_report(*, repo_root):
        return {
            "generated_at": "2026-05-04T10:00:00-04:00",
            "day": "20260504",
            "recommendation": "HOLD",
            "failures": [
                {
                    "issue_key": (
                        "data_availability:qqq_intraday_001:QQQ:"
                        "strategy_tradability:entry:stable_entry_guard:data_integrity_fail_closed"
                    )
                }
            ],
            "system_health": {"state": "recovering"},
            "runtime_reconciliation": {"blockers": []},
            "position_safety": {"go_no_go": "go"},
            "manual_intervention_locks": {"active_count": 0},
            "platform_eod": {"state": "VERIFIED_FLAT"},
            "qqq_liveness": {"tradable": False},
            "run_system": {"alive": True},
            "telegram": {"available": True},
            "market_calendar": {"trading_day": True},
        }

    monkeypatch.setattr(watchdog, "build_runtime_operations_watchdog_report", fake_report)

    class _Controller:
        state = "recovering"
        last_event = {}

        def __init__(self) -> None:
            self.published = []
            self.heartbeats = []

        async def publish(self, **kwargs) -> None:
            self.published.append(kwargs)

        def emit_heartbeat(self, **kwargs) -> None:
            self.heartbeats.append(kwargs)

    controller = _Controller()
    emitter = ProductionStateEmitter(repo_root=tmp_path, enabled=True, interval_sec=30, system_health_controller=controller)

    state = await emitter.run_once()

    assert state["go_no_go"] == "go"
    assert state["safety_go_no_go"] == "go"
    assert state["entry_go_no_go"] == "no_go"
    assert controller.published[-1]["event_type"] == "system_resumed"
    assert controller.published[-1]["reason"] == "production_state_go"
    assert controller.heartbeats == []


def test_data_availability_blocks_entries_but_not_safety_contract() -> None:
    service = DataAvailabilityService()
    gate = service.mark_unavailable(
        dataset="market_bars",
        symbol="QQQ",
        resolution="1m",
        reason="bars_async_fallback_empty",
        source="base",
        strategy_id="qqq_intraday_001",
    )

    assert gate.severity == "entry_blocking"
    assert service.entry_allowed(strategy_id="qqq_intraday_001") is False
    snapshot = service.snapshot(now=datetime(2026, 5, 4, 10, 0, tzinfo=ET))
    assert snapshot["entry_blocking_count"] == 1
    assert snapshot["platform_entry_blocking_count"] == 1

    service.clear(
        dataset="market_bars",
        symbol="QQQ",
        resolution="1m",
        strategy_id="qqq_intraday_001",
    )
    assert service.entry_allowed(strategy_id="qqq_intraday_001") is True


def test_data_availability_gate_expires_when_not_refreshed() -> None:
    service = DataAvailabilityService(gate_ttl_sec=60)
    now = datetime.now().astimezone()
    service.mark_unavailable(
        dataset="market_bars",
        symbol="SMH",
        resolution="1m",
        reason="bars_async_fallback_empty",
        source="base",
        now=now - timedelta(seconds=120),
    )

    snapshot = service.snapshot(now=now)

    assert snapshot["ok"] is True
    assert snapshot["active_gate_count"] == 0
    assert service.entry_allowed() is True


def test_data_availability_replays_durable_gates_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "data_availability_gates.json"
    service = DataAvailabilityService(gate_ttl_sec=300, durable_state_path=path)
    service.mark_unavailable(
        dataset="market_bars",
        symbol="QQQ",
        resolution="1m",
        reason="data_integrity_fail_closed",
        source="stable_entry_guard",
        strategy_id="qqq_intraday_001",
        now=datetime.now(ET),
    )

    restarted = DataAvailabilityService(gate_ttl_sec=300, durable_state_path=path)
    snapshot = restarted.snapshot(now=datetime(2026, 5, 4, 10, 1, tzinfo=ET))

    assert snapshot["ok"] is False
    assert snapshot["entry_blocking_count"] == 1
    assert snapshot["gates"][0]["reason"] == "data_integrity_fail_closed"


def test_data_availability_durable_write_failure_blocks_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "durable_state_dir"
    path.mkdir()
    service = DataAvailabilityService(gate_ttl_sec=300, durable_state_path=path)

    service.mark_unavailable(
        dataset="market_bars",
        symbol="QQQ",
        resolution="1m",
        reason="data_integrity_fail_closed",
        source="stable_entry_guard",
        strategy_id="qqq_intraday_001",
        now=datetime.now(ET),
    )
    snapshot = service.snapshot(now=datetime.now(ET))

    assert snapshot["ok"] is False
    assert snapshot["durable_state_write_failed"] is True
    assert service.entry_allowed() is False


def test_global_data_availability_service_can_be_wired_to_durable_state(tmp_path: Path) -> None:
    path = tmp_path / "global_data_availability_gates.json"
    service = configure_data_availability_service(durable_state_path=path)

    assert service is get_data_availability_service()
    assert service._durable_state_path == path


def test_data_availability_ignores_non_contract_market_bars() -> None:
    from types import SimpleNamespace

    service = DataAvailabilityService(gate_ttl_sec=300)
    service.set_strategy_data_contracts(
        [
            SimpleNamespace(
                symbols=("SMH",),
                requirements=(
                    SimpleNamespace(dataset="ticks", resolution="", symbols=("SMH",), rth=False),
                    SimpleNamespace(dataset="market_bars", resolution="5s", symbols=("SMH",), rth=False),
                ),
            )
        ]
    )
    service.mark_unavailable(
        dataset="market_bars",
        symbol="SMH",
        resolution="1m",
        reason="bars_async_fallback_empty",
        source="base",
    )
    service.mark_unavailable(
        dataset="market_bars",
        symbol="SMH",
        resolution="5s",
        reason="bars_async_fallback_empty",
        source="base",
    )

    snapshot = service.snapshot(now=datetime(2026, 5, 4, 23, 0, tzinfo=ET))

    assert snapshot["active_gate_count"] == 1
    assert snapshot["ignored_gate_count"] == 1
    assert snapshot["gates"][0]["resolution"] == "5s"
    assert snapshot["ignored_gates"][0]["resolution"] == "1m"
    assert service.entry_allowed() is False


def test_data_availability_ignores_rth_only_contract_gate_after_hours() -> None:
    service = DataAvailabilityService(gate_ttl_sec=300)
    service.set_strategy_data_contracts(
        [
            SimpleNamespace(
                production_blocking=True,
                strategy_id="stocks_kernel_smh_001",
                symbols=("SMH",),
                requirements=(
                    SimpleNamespace(dataset="market_bars", resolution="1m", symbols=("SMH",), rth=True),
                ),
            )
        ]
    )
    after_hours = datetime(2026, 5, 4, 23, 0, tzinfo=ET)
    service.mark_unavailable(
        dataset="market_bars",
        symbol="SMH",
        resolution="1m",
        reason="bars_async_fallback_empty",
        source="base",
        now=after_hours,
    )
    service.mark_unavailable(
        dataset="strategy_tradability",
        symbol="SMH",
        resolution="entry",
        strategy_id="stocks_kernel_smh_001",
        stage="required_data_missing",
        reason="history_missing",
        source="stocks_kernel",
        now=after_hours,
    )

    snapshot = service.snapshot(now=after_hours)

    assert snapshot["ok"] is True
    assert snapshot["active_gate_count"] == 0
    assert snapshot["ignored_gate_count"] == 2


def test_data_availability_keeps_all_session_contract_gate_active_after_hours() -> None:
    service = DataAvailabilityService(gate_ttl_sec=300)
    service.set_strategy_data_contracts(
        [
            SimpleNamespace(
                production_blocking=True,
                strategy_id="nvda_minute_flip_paper_001",
                symbols=("NVDA",),
                requirements=(
                    SimpleNamespace(dataset="ticks", resolution="", symbols=("NVDA",), rth=False),
                ),
            )
        ]
    )
    after_hours = datetime(2026, 5, 4, 23, 0, tzinfo=ET)
    service.mark_unavailable(
        dataset="ticks",
        symbol="NVDA",
        reason="tick_stale",
        source="market_data",
        now=after_hours,
    )

    snapshot = service.snapshot(now=after_hours)

    assert snapshot["ok"] is False
    assert snapshot["active_gate_count"] == 1
    assert snapshot["ignored_gate_count"] == 0


def test_data_availability_emits_listener_and_audit_on_gate_changes() -> None:
    class _Writer:
        def __init__(self) -> None:
            self.records = []

        def emit(self, **kwargs):
            self.records.append(kwargs)
            return True

    writer = _Writer()
    events = []
    service = DataAvailabilityService(gate_ttl_sec=300)
    service.set_audit_writer(writer)
    service.add_listener(events.append)

    service.mark_unavailable(
        dataset="strategy_tradability",
        symbol="QQQ",
        resolution="entry",
        reason="data_integrity_fail_closed",
        strategy_id="qqq_intraday_001",
        stage="stable_entry_guard",
    )
    service.clear(
        dataset="strategy_tradability",
        symbol="QQQ",
        resolution="entry",
        strategy_id="qqq_intraday_001",
        stage="stable_entry_guard",
    )

    assert [event["event_type"] for event in events] == ["data_unavailable", "data_available"]
    assert [record["stage"] for record in writer.records] == ["mark_unavailable", "clear"]
    assert writer.records[0]["component"] == "data_availability"


def test_recovery_readiness_fans_out_data_availability_changes() -> None:
    data_service = DataAvailabilityService(gate_ttl_sec=300)
    service = RecoveryReadinessService(data_availability_service=data_service)
    events = []
    service.add_listener(events.append)

    data_service.mark_unavailable(
        dataset="strategy_tradability",
        symbol="QQQ",
        resolution="entry",
        reason="feature_seed_missing",
        strategy_id="qqq_intraday_001",
    )

    assert events
    assert events[-1]["component"] == "data_availability"
    assert events[-1]["ready"] is True
    assert events[-1]["metadata"]["snapshot"]["active_gate_count"] == 1


def test_recovery_readiness_ignores_strategy_tradability_gate_for_platform_resume() -> None:
    data_service = DataAvailabilityService(gate_ttl_sec=300)
    now = datetime(2026, 5, 4, 10, 0, tzinfo=ET)

    data_service.mark_unavailable(
        dataset="strategy_tradability",
        symbol="QQQ",
        resolution="entry",
        reason="data_integrity_fail_closed",
        strategy_id="qqq_intraday_001",
        stage="stable_entry_guard",
        now=now,
    )
    service = RecoveryReadinessService(data_availability_service=data_service)

    snapshot = service.snapshot(now=now)

    assert snapshot["ready"] is True
    assert snapshot["data_availability"]["entry_blocking_count"] == 1
    assert snapshot["data_availability"]["platform_entry_blocking_count"] == 0
    assert snapshot["blockers"] == []


def test_recovery_readiness_still_blocks_market_data_gate_for_platform_resume() -> None:
    data_service = DataAvailabilityService(gate_ttl_sec=300)
    now = datetime(2026, 5, 4, 10, 0, tzinfo=ET)

    data_service.mark_unavailable(
        dataset="market_bars",
        symbol="QQQ",
        resolution="1m",
        reason="bars_async_fallback_empty",
        strategy_id="qqq_intraday_001",
        now=now,
    )
    service = RecoveryReadinessService(data_availability_service=data_service)

    snapshot = service.snapshot(now=now)

    assert snapshot["ready"] is False
    assert snapshot["data_availability"]["platform_entry_blocking_count"] == 1
    assert "data_availability_entry_blocking" in snapshot["blockers"]


def test_strategy_data_contract_parses_explicit_requirements_and_paper_live_blocking() -> None:
    contracts = build_strategy_data_contracts(
        {
            "strategies": [
                {
                    "id": "live_001",
                    "enabled": True,
                    "params": {
                        "extras": {
                            "data_contract": {
                                "production_blocking": True,
                                "symbols": ["QQQ"],
                                "requirements": [
                                    {"dataset": "ticks", "symbols": ["QQQ"], "ttl_sec": 10},
                                    {
                                        "dataset": "market_bars",
                                        "resolution": "1m",
                                        "symbols": ["QQQ"],
                                        "warmup_required": True,
                                        "min_rows": 600,
                                    },
                                ],
                            }
                        }
                    },
                },
                {
                    "id": "paper_live_002",
                    "enabled": True,
                    "params": {
                        "extras": {
                            "signal_symbol": "SMH",
                            "long_exec_symbol": "SOXL",
                            "data_contract": {
                                "requirements": [
                                    {"dataset": "market_bars", "resolution": "1m", "symbols": ["SMH", "SOXL"]}
                                ]
                            },
                        }
                    },
                },
            ]
        }
    )

    live = next(item for item in contracts if item.strategy_id == "live_001")
    paper_live = next(item for item in contracts if item.strategy_id == "paper_live_002")
    assert live.production_blocking is True
    assert paper_live.production_blocking is True
    assert "bars_1m" in live.datasets
    warmup = next(item for item in live.requirements if item.dataset == "bars_1m")
    assert warmup.warmup_required is True
    assert warmup.min_rows == 600

    service = DataAvailabilityService(gate_ttl_sec=300)
    service.set_strategy_data_contracts(contracts)
    service.mark_unavailable(dataset="market_bars", symbol="SMH", resolution="1m", reason="history_missing")
    service.mark_unavailable(dataset="market_bars", symbol="QQQ", resolution="1m", reason="history_missing")

    snapshot = service.snapshot(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))
    assert snapshot["active_gate_count"] == 2
    assert snapshot["ignored_gate_count"] == 0
    assert {gate["symbol"] for gate in snapshot["gates"]} == {"QQQ", "SMH"}


def test_strategy_tradability_snapshot_blocks_active_strategy_gate() -> None:
    config = {
        "strategies": [
            {
                "id": "stocks_kernel_smh_001",
                "enabled": True,
                "params": {
                    "extras": {
                        "data_contract": {"symbols": ["SMH"], "datasets": ["ticks"]},
                    }
                },
            }
        ]
    }
    service = DataAvailabilityService(gate_ttl_sec=300)
    service.set_strategy_data_contracts(build_strategy_data_contracts(config))
    rth_now = datetime(2026, 5, 4, 10, 0, tzinfo=ET)
    service.mark_unavailable(
        dataset="strategy_tradability",
        symbol="SMH",
        resolution="entry",
        strategy_id="stocks_kernel_smh_001",
        stage="feature_not_ready",
        reason="feature_seed_missing",
        now=rth_now,
    )

    snapshot = build_strategy_tradability_snapshot(
        config=config,
        generated_at=datetime(2026, 5, 4, 10, 0),
        data_availability=service.snapshot(now=rth_now),
    )

    assert snapshot["go_no_go"] == "no_go"
    assert snapshot["not_tradable_count"] == 1
    assert snapshot["blockers"] == ["stocks_kernel_smh_001:feature_not_ready:feature_seed_missing"]


def test_strategy_tradability_snapshot_blocks_qqq_not_tradable() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["QQQ"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 4, 10, 0),
        qqq_liveness={"tradable": False, "blocking_reasons": ["stream_invalid"]},
    )

    assert snapshot["go_no_go"] == "no_go"


def test_strategy_tradability_snapshot_requires_fresh_heartbeat_when_enabled() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "stocks_kernel_smh_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["SMH"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 4, 10, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={},
    )

    assert snapshot["go_no_go"] == "no_go"
    assert snapshot["blockers"] == ["stocks_kernel_smh_001:fresh_tradability_heartbeat_missing"]


def test_strategy_tradability_snapshot_ignores_derived_system_paused_heartbeat() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "stocks_kernel_smh_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["SMH"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 8, 11, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "stocks_kernel_smh_001": {
                "strategy_id": "stocks_kernel_smh_001",
                "tradable": False,
                "blockers": ["system_paused"],
                "details": {"reason": "system_paused"},
            }
        },
    )

    assert snapshot["go_no_go"] == "go"
    assert snapshot["not_tradable_count"] == 0
    assert snapshot["tradable_entry_ready_count"] == 0
    assert snapshot["blockers"] == []
    assert snapshot["records"][0]["tradable"] is False
    assert snapshot["records"][0]["system_ready"] is False


def test_strategy_tradability_snapshot_keeps_real_blocker_with_system_paused() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["QQQ"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 8, 11, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "qqq_intraday_001": {
                "strategy_id": "qqq_intraday_001",
                "tradable": False,
                "blockers": ["system_paused"],
                "details": {"reason": "system_paused"},
            }
        },
        qqq_liveness={"tradable": False, "blocking_reasons": ["system_paused", "stream_invalid"]},
    )

    assert snapshot["go_no_go"] == "no_go"
    assert snapshot["blockers"] == ["qqq_intraday_001:stream_invalid"]


def test_strategy_tradability_snapshot_accepts_flat_idle_outside_entry_window() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "nvda_option_churn_smoke_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["NVDA"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 5, 21, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "nvda_option_churn_smoke_001": {
                "strategy_id": "nvda_option_churn_smoke_001",
                "tradable": False,
                "blockers": ["idle_not_in_entry_window"],
                "details": {"idle_ok": True, "has_position": False, "has_pending": False},
            }
        },
    )

    assert snapshot["go_no_go"] == "go"
    assert snapshot["not_tradable_count"] == 0
    assert snapshot["idle_count"] == 1
    assert snapshot["tradable_entry_ready_count"] == 0
    assert snapshot["records"][0]["source"] == "heartbeat_idle"
    assert snapshot["records"][0]["tradable"] is False
    assert snapshot["records"][0]["tradable_entry_ready"] is False
    assert snapshot["records"][0]["idle_reason"] == "not_in_entry_window"


def test_strategy_tradability_snapshot_blocks_idle_when_position_exists() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "nvda_option_churn_smoke_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["NVDA"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 5, 21, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "nvda_option_churn_smoke_001": {
                "strategy_id": "nvda_option_churn_smoke_001",
                "tradable": False,
                "blockers": ["idle_not_in_entry_window"],
                "details": {"idle_ok": True, "has_position": True, "has_pending": False},
            }
        },
    )

    assert snapshot["go_no_go"] == "no_go"
    assert snapshot["blockers"] == ["nvda_option_churn_smoke_001:idle_not_in_entry_window"]


def test_strategy_tradability_snapshot_accepts_supervised_idle_position() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "nvda_option_churn_smoke_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["NVDA"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 5, 21, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "nvda_option_churn_smoke_001": {
                "strategy_id": "nvda_option_churn_smoke_001",
                "tradable": False,
                "blockers": ["idle_not_in_entry_window"],
                "details": {
                    "idle_ok": True,
                    "has_position": True,
                    "has_pending": False,
                    "safety_supervised": True,
                    "position_safety_owner": "platform_eod_position_safety",
                },
            }
        },
    )

    assert snapshot["go_no_go"] == "go"
    assert snapshot["not_tradable_count"] == 0
    assert snapshot["idle_count"] == 1
    assert snapshot["records"][0]["source"] == "heartbeat_idle"


def test_strategy_tradability_offsession_alive_not_tradable_entry_ready() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["QQQ"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 5, 21, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "qqq_intraday_001": {
                "strategy_id": "qqq_intraday_001",
                "tradable": False,
                "blockers": ["idle_not_in_entry_window"],
                "details": {
                    "idle_ok": True,
                    "has_position": False,
                    "has_pending": False,
                    "strategy_session_open": False,
                },
            }
        },
    )

    record = snapshot["records"][0]
    assert snapshot["go_no_go"] == "go"
    assert snapshot["idle_count"] == 1
    assert record["alive"] is True
    assert record["session_open"] is False
    assert record["tradable_entry_ready"] is False
    assert record["idle_reason"] == "offsession"


def test_strategy_tradability_stream_invalid_blocks_data_ready_and_entry_ready() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["QQQ"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 5, 10, 0),
        qqq_liveness={
            "tradable_entry_ready": False,
            "tradable": False,
            "blocking_reasons": ["stream_invalid"],
        },
    )

    record = snapshot["records"][0]
    assert snapshot["go_no_go"] == "no_go"
    assert record["data_ready"] is False
    assert record["tradable_entry_ready"] is False
    assert record["data_blockers"] == ["stream_invalid"]


def test_strategy_tradability_broker_truth_missing_blocks_risk_ready() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "stocks_kernel_smh_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["SMH"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 5, 10, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "stocks_kernel_smh_001": {
                "strategy_id": "stocks_kernel_smh_001",
                "tradable": False,
                "blockers": ["broker_truth_missing"],
                "details": {},
            }
        },
    )

    record = snapshot["records"][0]
    assert snapshot["go_no_go"] == "no_go"
    assert record["risk_ready"] is False
    assert record["tradable_entry_ready"] is False
    assert record["risk_blockers"] == ["broker_truth_missing"]


def test_strategy_tradability_close_only_allowed_while_entry_blocked() -> None:
    snapshot = build_strategy_tradability_snapshot(
        config={
            "strategies": [
                {
                    "id": "stocks_kernel_smh_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["SMH"], "datasets": ["ticks"]}}},
                }
            ]
        },
        generated_at=datetime(2026, 5, 5, 10, 0),
        require_fresh_heartbeat=True,
        strategy_heartbeats={
            "stocks_kernel_smh_001": {
                "strategy_id": "stocks_kernel_smh_001",
                "tradable": False,
                "blockers": ["broker_truth_missing"],
                "details": {"close_only_allowed": True},
            }
        },
    )

    assert snapshot["tradable_entry_ready_count"] == 0
    assert snapshot["close_only_allowed_count"] == 1
    assert snapshot["records"][0]["close_only_allowed"] is True


def test_strategy_tradability_heartbeat_read_write_and_ttl(tmp_path: Path) -> None:
    now = datetime(2026, 5, 4, 10, 0, tzinfo=ET)
    write_strategy_tradability_heartbeat(
        strategy_id="paper_live_001",
        tradable=True,
        source="unit_test",
        details={"symbol": "NVDA"},
        repo_root=tmp_path,
        now=now,
    )

    fresh = read_strategy_tradability_heartbeats(repo_root=tmp_path, now=now + timedelta(seconds=30), max_age_sec=90)
    assert fresh["paper_live_001"]["tradable"] is True
    assert fresh["paper_live_001"]["stale"] is False

    stale = read_strategy_tradability_heartbeats(repo_root=tmp_path, now=now + timedelta(seconds=120), max_age_sec=90)
    assert stale["paper_live_001"]["tradable"] is False
    assert stale["paper_live_001"]["stale"] is True
    assert "fresh_tradability_heartbeat_stale" in stale["paper_live_001"]["blockers"]


def test_strategy_production_manifest_gate_blocks_missing_manifest() -> None:
    validation = validate_strategy_production_manifests(
        {
            "strategies": [
                {
                    "id": "paper_live_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["QQQ"]}}},
                }
            ]
        },
        mandatory_strategy_ids=["paper_live_001"],
    )

    assert validation.ok is False
    assert validation.missing_strategy_ids == ("paper_live_001",)
    assert "paper_live_001:production_manifest_missing" in validation.blockers


def test_strategy_production_manifest_gate_requires_hard_contract_fields() -> None:
    validation = validate_strategy_production_manifests(
        {
            "strategies": [
                {
                    "id": "paper_live_001",
                    "enabled": True,
                    "params": {
                        "extras": {
                            "data_contract": {"symbols": ["QQQ"]},
                            "production_manifest": {
                                "production_blocking": True,
                                "entry_contract": {"uses_decision_pipeline": True, "uom_only": True},
                                "protection_contract": {"required": True, "fallback": "flatten"},
                                "recovery_contract": {
                                    "policy_id": "stocks_kernel_close_only",
                                    "unknown_owner_action": "quarantine",
                                    "fallback": "flatten",
                                },
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True},
                                "tradability_reporting": {"reports_strategy_tradability_gate": True},
                            },
                        }
                    },
                }
            ]
        },
        mandatory_strategy_ids=["paper_live_001"],
    )

    assert validation.ok is True
    assert validation.blockers == ()


def test_strategy_production_manifest_gate_blocks_unknown_recovery_policy() -> None:
    validation = validate_strategy_production_manifests(
        {
            "strategies": [
                {
                    "id": "paper_live_001",
                    "enabled": True,
                    "params": {
                        "extras": {
                            "data_contract": {"symbols": ["QQQ"]},
                            "production_manifest": {
                                "production_blocking": True,
                                "entry_contract": {"uses_decision_pipeline": True, "uom_only": True},
                                "protection_contract": {"required": True, "fallback": "flatten"},
                                "recovery_contract": {
                                    "policy_id": "unknown_policy",
                                    "unknown_owner_action": "quarantine",
                                    "fallback": "flatten",
                                },
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True},
                                "tradability_reporting": {"reports_strategy_tradability_gate": True},
                            },
                        }
                    },
                }
            ]
        },
        mandatory_strategy_ids=["paper_live_001"],
    )

    assert validation.ok is False
    assert "paper_live_001:recovery_policy_unknown" in validation.blockers


def test_recovery_readiness_required_component_expires_by_ttl() -> None:
    service = RecoveryReadinessService()
    now = datetime.now().astimezone()
    service.mark(
        "bar_pipeline",
        ready=True,
        reason="bar_published",
        metadata={"ttl_sec": 5},
        now=now - timedelta(seconds=10),
    )

    snapshot = service.snapshot(required_components=["bar_pipeline"], now=now)

    assert snapshot["ready"] is False
    assert "bar_pipeline_stale" in snapshot["blockers"]
    assert snapshot["components"]["bar_pipeline"]["stale"] is True


def test_recovery_readiness_allows_offhours_market_data_stale() -> None:
    service = RecoveryReadinessService()
    now = datetime(2026, 5, 8, 21, 0, tzinfo=ET)
    service.mark(
        "market_data_service",
        ready=True,
        reason="market_data_tick_heartbeat",
        metadata={"ttl_sec": 5, "allow_offhours_stale": True},
        now=now - timedelta(seconds=60),
    )

    snapshot = service.snapshot(required_components=["market_data_service"], now=now)

    assert snapshot["ready"] is True
    component = snapshot["components"]["market_data_service"]
    assert component["stale"] is True
    assert component["stale_allowed"] is True
    assert component["session_phase"] == "CLOSED"


def test_recovery_readiness_blocks_rth_market_data_stale() -> None:
    service = RecoveryReadinessService()
    now = datetime(2026, 5, 8, 10, 0, tzinfo=ET)
    service.mark(
        "market_data_service",
        ready=True,
        reason="market_data_tick_heartbeat",
        metadata={"ttl_sec": 5, "allow_offhours_stale": True},
        now=now - timedelta(seconds=60),
    )

    snapshot = service.snapshot(required_components=["market_data_service"], now=now)

    assert snapshot["ready"] is False
    assert "market_data_service_stale" in snapshot["blockers"]
    assert snapshot["components"]["market_data_service"]["session_phase"] == "RTH"


def test_market_data_subscription_readiness_requires_all_configured_symbols() -> None:
    readiness = get_recovery_readiness_service()
    readiness.clear()


def test_market_data_subscription_readiness_excludes_deferred_symbols() -> None:
    readiness = get_recovery_readiness_service()
    readiness.clear()

    class _Adapter:
        _deferred_symbols = {"AD-NASD"}

        def get_subscribed_symbols(self):
            return ["QQQ", "SPY"]

        def is_connected(self):
            return True

    service = MarketDataService.__new__(MarketDataService)
    service.symbols = ["QQQ", "SPY", "AD-NASD"]
    service.ib_adapter = _Adapter()
    service._last_recovery_data_plane_heartbeat_ms = 0

    service._refresh_recovery_data_plane_heartbeat(symbol="QQQ")
    snapshot = readiness.snapshot(required_components=["market_data_subscriptions"])

    assert snapshot["ready"] is True
    component = snapshot["components"]["market_data_subscriptions"]
    assert component["metadata"]["required_subscriptions"] == ["QQQ", "SPY", "AD-NASD"]
    assert component["metadata"]["effective_required_subscriptions"] == ["QQQ", "SPY"]
    assert component["metadata"]["deferred_subscriptions"] == ["AD-NASD"]

    readiness.clear()

    class _Adapter:
        def get_subscribed_symbols(self):
            return ["QQQ"]

        def is_connected(self):
            return True

    service = MarketDataService.__new__(MarketDataService)
    service.symbols = ["QQQ", "SPY", "TQQQ"]
    service.ib_adapter = _Adapter()
    service._last_recovery_data_plane_heartbeat_ms = 0

    service._refresh_recovery_data_plane_heartbeat(symbol="QQQ")
    snapshot = readiness.snapshot(required_components=["market_data_subscriptions"])

    assert snapshot["ready"] is False
    assert "market_data_subscriptions_not_ready" in snapshot["blockers"]
    component = snapshot["components"]["market_data_subscriptions"]
    assert component["metadata"]["required_subscriptions"] == ["QQQ", "SPY", "TQQQ"]
    assert component["metadata"]["missing_subscriptions"] == ["SPY", "TQQQ"]

    readiness.clear()


def test_production_readiness_requires_loaded_live_strategy_and_eod_supervisor() -> None:
    gate = ProductionReadinessGate().evaluate(
        exec_enabled=True,
        market_ready=True,
        exec_ready=True,
        master_ready=True,
        master_truth_complete=True,
        decision_pipeline_ready=True,
        execution_service_ready=True,
        runtime_reconciler_ready=True,
        runtime_reconcile_clean=True,
        eod_supervisor_ready=False,
        loaded_strategy_ids=[],
        mandatory_live_strategy_ids=["qqq_intraday_001"],
    )

    assert gate["production_ready"] is False
    assert "platform_eod_supervisor_not_ready" in gate["blockers"]
    assert "no_live_strategies_loaded" in gate["blockers"]


def test_production_readiness_requires_clean_runtime_reconcile() -> None:
    gate = ProductionReadinessGate().evaluate(
        exec_enabled=True,
        market_ready=True,
        exec_ready=True,
        master_ready=True,
        master_truth_complete=True,
        decision_pipeline_ready=True,
        execution_service_ready=True,
        runtime_reconciler_ready=True,
        runtime_reconcile_clean=False,
        runtime_reconcile_evidence={"ghosts_found": 1},
        eod_supervisor_ready=True,
        loaded_strategy_ids=["qqq_intraday_001"],
        mandatory_live_strategy_ids=["qqq_intraday_001"],
    )

    assert gate["production_ready"] is False
    assert "runtime_reconcile_not_clean" in gate["blockers"]
    assert gate["runtime_reconcile_evidence"]["ghosts_found"] == 1


def test_production_readiness_requires_decision_pipeline() -> None:
    gate = ProductionReadinessGate().evaluate(
        exec_enabled=True,
        market_ready=True,
        exec_ready=True,
        master_ready=True,
        master_truth_complete=True,
        decision_pipeline_ready=False,
        execution_service_ready=True,
        runtime_reconciler_ready=True,
        runtime_reconcile_clean=True,
        runtime_reconcile_evidence={"active_quarantine_count": 0},
        eod_supervisor_ready=True,
        loaded_strategy_ids=["qqq_intraday_001"],
        mandatory_live_strategy_ids=["qqq_intraday_001"],
    )

    assert gate["production_ready"] is False
    assert "decision_pipeline_not_ready" in gate["blockers"]


def test_production_readiness_blocks_active_quarantine_evidence() -> None:
    gate = ProductionReadinessGate().evaluate(
        exec_enabled=True,
        market_ready=True,
        exec_ready=True,
        master_ready=True,
        master_truth_complete=True,
        decision_pipeline_ready=True,
        execution_service_ready=True,
        runtime_reconciler_ready=True,
        runtime_reconcile_clean=False,
        runtime_reconcile_evidence={"active_quarantine_count": 1},
        eod_supervisor_ready=True,
        loaded_strategy_ids=["qqq_intraday_001"],
        mandatory_live_strategy_ids=["qqq_intraday_001"],
    )

    assert gate["production_ready"] is False
    assert "runtime_reconcile_not_clean" in gate["blockers"]


class _FakeFlattenResult:
    scope_mode = "ACCOUNT_ALL"
    verification_state = "VERIFIED_FLAT"
    verified_flat = True
    total_orders = 0
    success_orders = 0
    failed_orders = 0
    db_targets = 0
    broker_fallback_targets = 0
    remaining_position_targets = 0
    remaining_open_orders = 0
    order_results = []


class _FakeManualFlattenService:
    def __init__(self, results=None) -> None:
        self.triggers = []
        self.strategy_triggers = []
        self.results = list(results or [])

    async def execute_flatten_all(self, *, trigger_id: str) -> _FakeFlattenResult:
        self.triggers.append(trigger_id)
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return _FakeFlattenResult()

    async def execute_flatten_for_strategy(self, *, trigger_id: str, strategy_id: str) -> _FakeFlattenResult:
        self.strategy_triggers.append({"trigger_id": trigger_id, "strategy_id": strategy_id})
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return _FakeFlattenResult()


@pytest.mark.asyncio
async def test_platform_eod_supervisor_emits_machine_checkable_artifact(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService()
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T16:00:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["verified_flat"] is True
    assert fake.triggers == ["platform_eod_flatten:2026-05-04"]
    path = tmp_path / "platform_eod_flatten_2026-05-04.json"
    assert path.exists()
    assert artifact["state"] == "VERIFIED_FLAT"
    assert artifact["attempt_count"] == 0


@pytest.mark.asyncio
async def test_platform_eod_supervisor_uses_session_close_policy_scope(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService()
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
        session_close_policies=[
            {
                "strategy_id": "rth_strategy",
                "strategy_instance_id": "rth_strategy_001",
                "symbol": "TQQQ",
                "policy_type": "RTH_ONLY_EQUITY",
                "flatten_enabled": True,
            },
            {
                "strategy_id": "extended_strategy",
                "strategy_instance_id": "extended_strategy_001",
                "symbol": "NVDA",
                "policy_type": "EXTENDED_EQUITY",
                "flatten_enabled": False,
            },
        ],
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T16:00:00-04:00"))

    assert fake.triggers == []
    assert fake.strategy_triggers == [
        {"trigger_id": "platform_eod_flatten:2026-05-04:rth_strategy", "strategy_id": "rth_strategy"}
    ]
    assert artifact["scope_mode"] == "POLICY_SCOPED"
    assert artifact["session_close_policy_mode"] == "POLICY_SCOPED"
    assert artifact["session_close_selected_strategy_ids"] == ["rth_strategy"]
    selected = [item for item in artifact["session_close_policies"] if item["selected_for_flatten"]]
    assert [item["strategy_instance_id"] for item in selected] == ["rth_strategy_001"]


@pytest.mark.asyncio
async def test_platform_eod_supervisor_accepts_policy_objects_from_config(tmp_path: Path) -> None:
    policies = session_close_policies_from_config(
        {
            "strategies": [
                {
                    "id": "rth_strategy",
                    "enabled": True,
                    "module": "strategies.stocks_kernel_strategy",
                    "class": "StocksKernelStrategy",
                    "params": {
                        "extras": {
                            "account_id": "DU123",
                            "data_contract": {"symbols": ["TQQQ"]},
                            "production_manifest": {
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True}
                            },
                        }
                    },
                },
                {
                    "id": "extended_strategy",
                    "enabled": True,
                    "module": "strategies.stocks_kernel_strategy",
                    "class": "StocksKernelStrategy",
                    "params": {
                        "extras": {
                            "account_id": "DU123",
                            "data_contract": {"symbols": ["NVDA"]},
                            "gate_overrides": {
                                "session_window": {"params": {"allowed_sessions": ["PRE", "POST", "OVERNIGHT"]}}
                            },
                            "production_manifest": {
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True}
                            },
                        }
                    },
                },
            ]
        }
    )
    fake = _FakeManualFlattenService()
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
        session_close_policies=policies,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T16:00:00-04:00"))

    assert artifact["session_close_policy_mode"] == "POLICY_SCOPED"
    assert artifact["session_close_selected_strategy_ids"] == ["rth_strategy"]
    assert fake.triggers == []
    assert fake.strategy_triggers == [
        {"trigger_id": "platform_eod_flatten:2026-05-04:rth_strategy", "strategy_id": "rth_strategy"}
    ]


def test_session_close_policies_from_config_keeps_extended_strategy_out_of_rth_flatten() -> None:
    policies = session_close_policies_from_config(
        {
            "strategies": [
                {
                    "id": "rth_strategy",
                    "enabled": True,
                    "module": "strategies.stocks_kernel_strategy",
                    "class": "StocksKernelStrategy",
                    "params": {
                        "extras": {
                            "account_id": "DU123",
                            "strategy_instance_id": "rth_strategy_001",
                            "data_contract": {"symbols": ["AAPL"]},
                            "production_manifest": {
                                "eod_contract": {
                                    "no_overnight": True,
                                    "platform_eod_required": True,
                                }
                            },
                        }
                    },
                },
                {
                    "id": "extended_strategy",
                    "enabled": True,
                    "module": "strategies.stocks_kernel_strategy",
                    "class": "StocksKernelStrategy",
                    "params": {
                        "extras": {
                            "account_id": "DU123",
                            "strategy_instance_id": "extended_strategy_001",
                            "data_contract": {"symbols": ["NVDA"]},
                            "gate_overrides": {
                                "session_window": {
                                    "params": {"allowed_sessions": ["PRE", "POST", "OVERNIGHT"]}
                                }
                            },
                            "production_manifest": {
                                "eod_contract": {
                                    "no_overnight": True,
                                    "platform_eod_required": True,
                                }
                            },
                        }
                    },
                },
            ]
        }
    )

    by_strategy = {policy.strategy_id: policy for policy in policies}

    assert by_strategy["rth_strategy"].policy_type == "RTH_ONLY_EQUITY"
    assert by_strategy["rth_strategy"].should_flatten() is True
    assert by_strategy["extended_strategy"].policy_type == "EXTENDED_EQUITY"
    assert by_strategy["extended_strategy"].should_flatten() is False


class _FakeUnverifiedFlattenResult(_FakeFlattenResult):
    verification_state = "SUBMITTED_NOT_VERIFIED"
    verified_flat = False
    total_orders = 1
    success_orders = 1
    failed_orders = 0
    remaining_position_targets = 1
    remaining_open_orders = 1

    def __init__(self) -> None:
        self.order_results = [
            SimpleNamespace(
                strategy_id="qqq_intraday_001",
                symbol="TQQQ",
                qty=10.0,
                success=True,
                decision_id="d-eod",
                reason="",
                stage="completed",
                order_id=101,
                idempotency_key="eod:1",
                side="SELL",
                session="RTH",
            )
        ]


class _FakeAwaitingMarketReopenFlattenResult(_FakeFlattenResult):
    verification_state = "SUBMIT_FAILED"
    verified_flat = False
    total_orders = 1
    success_orders = 0
    failed_orders = 1
    remaining_position_targets = 1
    remaining_open_orders = 0

    def __init__(self) -> None:
        self.order_results = [
            SimpleNamespace(
                strategy_id="nvda_minute_flip_paper_001",
                symbol="NVDA",
                qty=1.0,
                success=False,
                decision_id="d-eod",
                reason="manual_flatten_awaiting_market_reopen",
                stage="validation",
                order_id=None,
                idempotency_key=None,
                side="SELL",
                session="overnight",
            )
        ]


class _FakeAwaitingOptionMarketReopenFlattenResult(_FakeFlattenResult):
    verification_state = "SUBMIT_FAILED"
    verified_flat = False
    total_orders = 1
    success_orders = 0
    failed_orders = 1
    remaining_position_targets = 1
    remaining_open_orders = 0

    def __init__(self) -> None:
        self.order_results = [
            SimpleNamespace(
                strategy_id="nvda_option_churn_smoke_001",
                symbol="OPT:NVDA:20260506:207.5:P",
                qty=1.0,
                success=False,
                decision_id="d-eod",
                reason="manual_flatten_awaiting_option_market_reopen",
                stage="validation",
                order_id=None,
                idempotency_key=None,
                side="SELL",
                session="overnight",
            )
        ]


class _FakeManualLockStore:
    def __init__(self, *, active_lock=None, active_locks=None) -> None:
        self.calls = []
        self.unlock_calls = []
        self.active_lock = dict(active_lock) if isinstance(active_lock, dict) else None
        self.active_locks = [dict(item) for item in active_locks or []]

    async def lock(self, **kwargs):
        self.calls.append(kwargs)
        return dict(kwargs)

    async def get_active(self, **kwargs):
        key = (
            str(kwargs.get("strategy_id") or "").strip(),
            str(kwargs.get("symbol") or "").strip().upper(),
        )
        if self.active_lock is not None:
            active_key = (
                str(self.active_lock.get("strategy_id") or "").strip(),
                str(self.active_lock.get("symbol") or "").strip().upper(),
            )
            if active_key == key or active_key == ("", ""):
                return dict(self.active_lock)
        for item in self.active_locks:
            active_key = (
                str(item.get("strategy_id") or "").strip(),
                str(item.get("symbol") or "").strip().upper(),
            )
            if active_key == key:
                return dict(item)
        return None

    async def list_active(self):
        rows = []
        if self.active_lock is not None:
            rows.append(dict(self.active_lock))
        rows.extend(dict(item) for item in self.active_locks)
        return rows

    async def unlock(self, **kwargs):
        self.unlock_calls.append(kwargs)
        existed = self.active_lock is not None
        key = (
            str(kwargs.get("strategy_id") or "").strip(),
            str(kwargs.get("symbol") or "").strip().upper(),
        )
        before_count = len(self.active_locks)
        self.active_locks = [
            item
            for item in self.active_locks
            if (
                str(item.get("strategy_id") or "").strip(),
                str(item.get("symbol") or "").strip().upper(),
            )
            != key
        ]
        existed = existed or len(self.active_locks) != before_count
        if self.active_lock is not None:
            existed = existed or (
                str(self.active_lock.get("strategy_id") or "").strip(),
                str(self.active_lock.get("symbol") or "").strip().upper(),
            ) == key
            self.active_lock = None
        return existed


@pytest.mark.asyncio
async def test_platform_eod_supervisor_retries_until_verified_flat(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[_FakeUnverifiedFlattenResult(), _FakeFlattenResult()])
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
        max_attempts_per_day=3,
    )
    now = datetime.fromisoformat("2026-05-04T16:00:00-04:00")

    first = await supervisor.run_once(now=now)
    assert first["go_no_go"] == "no_go"
    assert first["state"] == "SUBMITTED_NOT_VERIFIED"
    assert supervisor._should_run(now) is True

    second = await supervisor.run_once(now=now)
    assert second["go_no_go"] == "go"
    assert second["state"] == "VERIFIED_FLAT"
    assert second["attempt_count"] == 1
    assert supervisor._should_run(now) is False
    assert supervisor._should_run(now.replace(minute=2)) is True
    assert len(fake.triggers) == 2


@pytest.mark.asyncio
async def test_platform_eod_supervisor_submission_exception_does_not_mark_day_complete(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[RuntimeError("submit down"), _FakeFlattenResult()])
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
        max_attempts_per_day=3,
    )
    now = datetime.fromisoformat("2026-05-04T16:00:00-04:00")

    first = await supervisor.run_once(now=now)
    assert first["go_no_go"] == "no_go"
    assert first["state"] == "RETRYING"
    assert first["error"] == "submit down"
    assert supervisor._should_run(now) is True

    second = await supervisor.run_once(now=now)
    assert second["state"] == "VERIFIED_FLAT"
    assert supervisor._should_run(now) is False


@pytest.mark.asyncio
async def test_platform_eod_supervisor_verified_flat_rechecks_do_not_consume_recovery_attempts(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(
        results=[_FakeFlattenResult(), _FakeFlattenResult(), _FakeUnverifiedFlattenResult()]
    )
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
        max_attempts_per_day=1,
    )
    now = datetime.fromisoformat("2026-05-04T16:00:00-04:00")

    first = await supervisor.run_once(now=now)
    recheck = await supervisor.run_once(now=now.replace(minute=2))
    later_position = await supervisor.run_once(now=now.replace(minute=4))

    assert first["state"] == "VERIFIED_FLAT"
    assert recheck["state"] == "VERIFIED_FLAT"
    assert recheck["attempt_count"] == 0
    assert later_position["state"] == "MANUAL_REQUIRED"
    assert later_position["attempt_count"] == 1


@pytest.mark.asyncio
async def test_platform_eod_supervisor_awaits_market_reopen_without_retry_spam(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[_FakeAwaitingMarketReopenFlattenResult()])
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        recovery_incident_service=incident_service,
        flatten_time_et="15:59:00",
        max_attempts_per_day=2,
    )
    now = datetime.fromisoformat("2026-05-04T21:00:00-04:00")

    artifact = await supervisor.run_once(now=now)

    assert artifact["go_no_go"] == "no_go"
    assert artifact["state"] == "AWAITING_MARKET_REOPEN"
    assert artifact["attempt_count"] == 0
    assert artifact["awaiting_market_reopen"] is True
    assert artifact["remaining_position_targets"] == 1
    index = json.loads((tmp_path / "recovery_incidents" / "recovery_incidents_latest.json").read_text())
    assert index["active_intent_count"] == 1
    assert index["incidents"][0]["state"] == "awaiting_market_reopen"
    assert index["incidents"][0]["active_recovery_intent"]["next_action"] == "wait_until_market_reopen"
    assert supervisor._should_run(now) is False
    assert fake.triggers == ["platform_eod_flatten:2026-05-04"]


@pytest.mark.asyncio
async def test_platform_eod_supervisor_delays_option_market_reopen_until_rth(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[_FakeAwaitingOptionMarketReopenFlattenResult()])
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        recovery_incident_service=incident_service,
        flatten_time_et="15:59:00",
        max_attempts_per_day=2,
    )
    now = datetime.fromisoformat("2026-05-04T21:00:00-04:00")

    artifact = await supervisor.run_once(now=now)

    assert artifact["state"] == "AWAITING_MARKET_REOPEN"
    assert artifact["attempt_count"] == 0
    assert artifact["recovery_incident_ids"] == [
        "recovery_incident:nvda_option_churn_smoke_001:OPT:NVDA:20260506:207.5:P"
    ]
    assert supervisor._awaiting_market_reopen_not_before_by_day["2026-05-04"].isoformat().startswith("2026-05-05T09:30:00")
    assert supervisor._should_run(datetime.fromisoformat("2026-05-04T20:30:00-04:00")) is False
    assert fake.triggers == ["platform_eod_flatten:2026-05-04"]


@pytest.mark.asyncio
async def test_platform_eod_supervisor_hydrates_awaiting_market_reopen_after_restart(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[_FakeAwaitingOptionMarketReopenFlattenResult()])
    first = PlatformEODSupervisor(
        manual_flatten_service=fake,
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
        max_attempts_per_day=2,
    )
    now = datetime.fromisoformat("2026-05-04T21:00:00-04:00")
    await first.run_once(now=now)

    restarted = PlatformEODSupervisor(
        manual_flatten_service=_FakeManualFlattenService(),
        artifact_dir=tmp_path,
        flatten_time_et="15:59:00",
        max_attempts_per_day=2,
    )

    assert restarted._should_run(datetime.fromisoformat("2026-05-04T21:01:00-04:00")) is False
    assert restarted._attempt_count_by_day["2026-05-04"] == 0


@pytest.mark.asyncio
async def test_platform_eod_supervisor_migrates_old_awaiting_market_reopen_artifact(tmp_path: Path) -> None:
    day = "2026-05-04"
    path = tmp_path / f"platform_eod_flatten_{day}.json"
    path.write_text(
        json.dumps(
            {
                "artifact_type": "platform_eod_flatten",
                "completed_at": "2026-05-04T21:00:00-04:00",
                "state": "AWAITING_MARKET_REOPEN",
                "attempt_count": 1,
                "order_results": [
                    {
                        "reason": "manual_flatten_awaiting_option_market_reopen",
                        "symbol": "OPT:NVDA:20260506:207.5:P",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=_FakeManualFlattenService(),
        artifact_dir=tmp_path,
        recovery_incident_service=RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents"),
        flatten_time_et="15:59:00",
        max_attempts_per_day=2,
    )

    assert supervisor._should_run(datetime.fromisoformat("2026-05-04T21:01:00-04:00")) is False

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["attempt_count"] == 0
    assert migrated["awaiting_market_reopen_not_before"].startswith("2026-05-05T09:30:00")
    assert migrated["recovery_incident_ids"] == [
        "recovery_incident:platform_eod_flatten:OPT:NVDA:20260506:207.5:P"
    ]


@pytest.mark.asyncio
async def test_platform_eod_supervisor_creates_manual_lock_after_max_attempts(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[_FakeUnverifiedFlattenResult(), _FakeUnverifiedFlattenResult()])
    lock_store = _FakeManualLockStore()
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        manual_intervention_lock_store=lock_store,
        artifact_dir=tmp_path / "eod",
        manual_lock_dir=tmp_path / "manual_locks",
        flatten_time_et="15:59:00",
        max_attempts_per_day=2,
    )
    now = datetime.fromisoformat("2026-05-04T16:00:00-04:00")

    first = await supervisor.run_once(now=now)
    second = await supervisor.run_once(now=now)

    assert first["state"] == "SUBMITTED_NOT_VERIFIED"
    assert second["state"] == "MANUAL_REQUIRED"
    assert second["manual_lock_ids"] == [
        "platform_eod_manual_lock:2026-05-04",
        "platform_eod_manual_lock:2026-05-04:qqq_intraday_001:TQQQ",
    ]
    assert supervisor._should_run(now) is False
    lock_path = tmp_path / "manual_locks" / "platform_eod_manual_lock_2026-05-04.json"
    assert lock_path.exists()
    assert lock_store.calls == [
        {
            "strategy_id": "qqq_intraday_001",
            "symbol": "TQQQ",
            "ttl_sec": 24 * 60 * 60,
            "reason": "platform_eod_manual_required",
            "context": {
                "lock_id": "platform_eod_manual_lock:2026-05-04:qqq_intraday_001:TQQQ",
                "trigger_id": "platform_eod_flatten:2026-05-04",
                "attempt_count": 2,
                "remaining_position_targets": 1,
                "remaining_open_orders": 1,
                "eod_reason": "platform_eod_flatten_not_verified",
                "error": "",
                "account_id": "",
                "strategy_instance_id": "qqq_intraday_001",
                "session": "RTH",
            },
        }
    ]
    incident_index = json.loads(
        (tmp_path / "recovery_incidents" / "recovery_incidents_latest.json").read_text(encoding="utf-8")
    )
    assert incident_index["active_count"] == 0
    assert incident_index["terminal_count"] == 1
    assert second["recovery_incident_ids"] == ["recovery_incident:qqq_intraday_001:TQQQ"]
    assert incident_index["incidents"][0]["state"] == "manual_required_acknowledged"


@pytest.mark.asyncio
async def test_platform_eod_supervisor_scoped_policy_manual_lock_has_no_day_global_lock(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[_FakeUnverifiedFlattenResult(), _FakeUnverifiedFlattenResult()])
    lock_store = _FakeManualLockStore()
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        manual_intervention_lock_store=lock_store,
        artifact_dir=tmp_path / "eod",
        manual_lock_dir=tmp_path / "manual_locks",
        flatten_time_et="15:59:00",
        max_attempts_per_day=2,
        session_close_policies=[
            {
                "strategy_id": "qqq_intraday_001",
                "strategy_instance_id": "qqq_intraday_001",
                "account_id": "DU123",
                "symbol": "TQQQ",
                "policy_type": "RTH_ONLY_EQUITY",
                "flatten_enabled": True,
            },
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "strategy_instance_id": "nvda_minute_flip_paper_001",
                "account_id": "DU123",
                "symbol": "NVDA",
                "policy_type": "EXTENDED_EQUITY",
                "flatten_enabled": True,
            },
        ],
    )
    now = datetime.fromisoformat("2026-05-04T16:00:00-04:00")

    await supervisor.run_once(now=now)
    second = await supervisor.run_once(now=now)

    assert second["state"] == "MANUAL_REQUIRED"
    assert second["manual_lock_ids"] == ["platform_eod_manual_lock:2026-05-04:qqq_intraday_001:TQQQ"]
    assert not (tmp_path / "manual_locks" / "platform_eod_manual_lock_2026-05-04.json").exists()
    assert (tmp_path / "manual_locks" / "platform_eod_manual_lock_2026-05-04_qqq_intraday_001_TQQQ.json").exists()
    assert lock_store.calls == [
        {
            "strategy_id": "qqq_intraday_001",
            "symbol": "TQQQ",
            "ttl_sec": 24 * 60 * 60,
            "reason": "platform_eod_manual_required",
            "context": {
                "lock_id": "platform_eod_manual_lock:2026-05-04:qqq_intraday_001:TQQQ",
                "trigger_id": "platform_eod_flatten:2026-05-04",
                "attempt_count": 2,
                "remaining_position_targets": 1,
                "remaining_open_orders": 1,
                "eod_reason": "platform_eod_flatten_not_verified",
                "error": "",
                "account_id": "",
                "strategy_instance_id": "qqq_intraday_001",
                "session": "RTH",
            },
        }
    ]


@pytest.mark.asyncio
async def test_platform_eod_supervisor_clears_manual_locks_after_verified_flat(tmp_path: Path) -> None:
    fake = _FakeManualFlattenService(results=[_FakeFlattenResult()])
    lock_store = _FakeManualLockStore(
        active_locks=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "reason": "platform_eod_manual_required",
                "context": {
                    "lock_id": "platform_eod_manual_lock:2026-05-04:nvda_minute_flip_paper_001:NVDA",
                    "trigger_id": "platform_eod_flatten:2026-05-04",
                },
            },
            {
                "strategy_id": "other_strategy",
                "symbol": "MSFT",
                "reason": "protection_failed",
                "context": {"trigger_id": "platform_eod_flatten:2026-05-04"},
            },
        ]
    )
    manual_lock_dir = tmp_path / "manual_locks"
    manual_lock_dir.mkdir(parents=True)
    stale_file = manual_lock_dir / "platform_eod_manual_lock_2026-05-04.json"
    stale_file.write_text(
        json.dumps(
            {
                "lock_id": "platform_eod_manual_lock:2026-05-04",
                "trigger_id": "platform_eod_flatten:2026-05-04",
                "reason": "platform_eod_flatten_not_verified",
            }
        ),
        encoding="utf-8",
    )
    supervisor = PlatformEODSupervisor(
        manual_flatten_service=fake,
        manual_intervention_lock_store=lock_store,
        artifact_dir=tmp_path / "eod",
        manual_lock_dir=manual_lock_dir,
        flatten_time_et="15:59:00",
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T16:00:00-04:00"))

    assert artifact["state"] == "VERIFIED_FLAT"
    assert artifact["manual_lock_ids"] == []
    assert {item["source"] for item in artifact["manual_locks_cleared"]} == {"file", "db"}
    assert lock_store.unlock_calls == [{"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA"}]
    assert not stale_file.exists()


class _FakePositionPg:
    def __init__(
        self,
        *,
        open_orders=None,
        protection_registry=None,
        quarantine=False,
        manual_locks=None,
        core_positions=None,
        ledger_positions=None,
        nonterminal_orders=None,
        quarantine_rows=None,
        quarantine_symbol="TQQQ",
        quarantine_reason="unknown_broker_order",
        quarantine_note=None,
    ) -> None:
        self.open_orders = list(open_orders or [])
        self.protection_registry = list(protection_registry or [])
        self.quarantine = bool(quarantine)
        self.manual_locks = list(manual_locks or [])
        self.core_positions = dict(core_positions or {})
        self.ledger_positions = dict(ledger_positions or {})
        self.nonterminal_orders = set(nonterminal_orders or set())
        self.quarantine_rows = [dict(row) for row in (quarantine_rows or [])]
        self.quarantine_symbol = str(quarantine_symbol or "TQQQ").upper()
        self.quarantine_reason = str(quarantine_reason or "unknown_broker_order")
        self.quarantine_note = quarantine_note
        self.executed = []

    async def fetchall(self, sql, *args):
        if "FROM core.core_protection_order_registry" in str(sql):
            return list(self.protection_registry)
        if "FROM core.core_order_quarantine" in str(sql):
            if self.quarantine_rows:
                return [dict(row) for row in self.quarantine_rows if str(row.get("status") or "active") == "active"]
            if self.quarantine:
                return [
                    {
                        "quarantine_id": "quarantine-1",
                        "symbol": self.quarantine_symbol,
                        "reason": self.quarantine_reason,
                        "note": self.quarantine_note,
                        "count": 1,
                        "total_count": 1,
                    }
                ]
            return []
        if "FROM core.manual_intervention_lock" in str(sql):
            return list(self.manual_locks)
        if "FROM core.core_position" in str(sql):
            return [
                {"strategy_id": strategy_id, "symbol": symbol, "pos_qty": qty}
                for (strategy_id, symbol), qty in self.core_positions.items()
                if abs(float(qty or 0.0)) > 1e-9
            ]
        if "FROM core.core_order\n" in str(sql) or "FROM core.core_order " in str(sql):
            return list(self.open_orders)
        if "FROM core.position_ledger_current" in str(sql):
            if "GROUP BY account_id, strategy_id, strategy_instance_id, symbol, lot_id" in str(sql):
                symbol = str(args[0]).upper() if args else ""
                rows = []
                for key, qty in self.ledger_positions.items():
                    if len(key) == 6:
                        account_id, strategy_id, strategy_instance_id, row_symbol, lot_id, position_owner_key = key
                    elif len(key) == 5:
                        account_id, strategy_id, strategy_instance_id, row_symbol, lot_id = key
                        position_owner_key = ""
                    elif len(key) == 3:
                        strategy_id, row_symbol, lot_id = key
                        account_id = ""
                        strategy_instance_id = strategy_id
                        position_owner_key = ""
                    else:
                        strategy_id, row_symbol = key
                        account_id = ""
                        strategy_instance_id = strategy_id
                        lot_id = ""
                        position_owner_key = ""
                    if str(row_symbol).upper() != symbol or abs(float(qty or 0.0)) <= 1e-9:
                        continue
                    rows.append(
                        {
                            "account_id": account_id,
                            "strategy_id": strategy_id,
                            "strategy_instance_id": strategy_instance_id,
                            "symbol": str(row_symbol).upper(),
                            "lot_id": lot_id,
                            "position_owner_key": position_owner_key,
                            "qty": float(qty or 0.0),
                        }
                    )
                return rows
        return []

    async def fetchone(self, sql, *args):
        if "FROM core.core_order_quarantine" in str(sql) and self.quarantine:
            symbol = str(args[0] if args else "").upper()
            return {"active": 1} if symbol == self.quarantine_symbol else None
        if "FROM core.core_position" in str(sql):
            if len(args) == 1:
                symbol = str(args[0]).upper()
                for (_strategy_id, row_symbol), qty in self.core_positions.items():
                    if row_symbol == symbol and abs(float(qty or 0.0)) > 1e-9:
                        return {"active": 1}
                return None
            key = (str(args[0]), str(args[1]).upper()) if len(args) >= 2 else ("", "")
            if key in self.core_positions:
                return {"pos_qty": self.core_positions[key]}
            return None
        if "FROM core.core_order\n" in str(sql) or "FROM core.core_order " in str(sql):
            if "broker_order_id = $1" in str(sql):
                broker_order_id = str(args[0]) if args else ""
                for order in self.open_orders:
                    if str(order.get("broker_order_id") or "") == broker_order_id:
                        return dict(order)
                return None
            if "order_ref = $1" in str(sql):
                order_ref = str(args[0]) if args else ""
                symbol = str(args[1]).upper() if len(args) >= 2 else ""
                for order in self.open_orders:
                    if str(order.get("order_ref") or "") != order_ref:
                        continue
                    if symbol and str(order.get("symbol") or "").upper() != symbol:
                        continue
                    return dict(order)
                return None
            if len(args) == 1:
                symbol = str(args[0]).upper()
                for order in self.open_orders:
                    if str(order.get("symbol") or "").upper() != symbol:
                        continue
                    if str(order.get("state") or "").upper() not in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED"}:
                        return {"active": 1}
                return None
            key = (str(args[0]), str(args[1]).upper()) if len(args) >= 2 else ("", "")
            return {"active": 1} if key in self.nonterminal_orders else None
        if "FROM core.position_ledger_current" in str(sql):
            if "owner_lane_qty" in str(sql):
                key = (str(args[0]), str(args[1]).upper(), str(args[2])) if len(args) >= 3 else ("", "", "")
                if key in self.ledger_positions:
                    return {"owner_lane_qty": abs(float(self.ledger_positions.get(key) or 0.0))}
                legacy_key = (str(args[0]), str(args[1]).upper()) if len(args) >= 2 else ("", "")
                return {"owner_lane_qty": abs(float(self.ledger_positions.get(legacy_key, 0.0) or 0.0))}
            if len(args) == 1:
                symbol = str(args[0]).upper()
                qty = 0.0
                for key, value in self.ledger_positions.items():
                    row_symbol = key[3] if len(key) in {5, 6} else key[1]
                    if str(row_symbol).upper() == symbol:
                        qty += float(value or 0.0)
                return {"ledger_qty": qty}
            if len(args) >= 2:
                strategy_id = str(args[0])
                symbol = str(args[1]).upper()
                qty = 0.0
                for key, value in self.ledger_positions.items():
                    if len(key) == 6:
                        _account_id, row_strategy_id, _instance_id, row_symbol, _lot_id, _owner_key = key
                    elif len(key) == 5:
                        _account_id, row_strategy_id, _instance_id, row_symbol, _lot_id = key
                    else:
                        row_strategy_id, row_symbol = key[:2]
                    if str(row_strategy_id) == strategy_id and str(row_symbol).upper() == symbol:
                        qty += float(value or 0.0)
                if abs(qty) > 1e-12:
                    return {"ledger_qty": qty}
            key = (str(args[0]), str(args[1]).upper()) if len(args) >= 2 else ("", "")
            return {"ledger_qty": self.ledger_positions.get(key, 0.0)}
        return None

    async def execute(self, sql, *args):
        self.executed.append((str(sql), args))
        if "UPDATE core.core_position" in str(sql) and len(args) >= 2:
            key = (str(args[0]), str(args[1]).upper())
            if key in self.core_positions:
                self.core_positions[key] = 0.0
        if "UPDATE core.core_order\n" in str(sql) and len(args) >= 1:
            order_id = str(args[0])
            for order in self.open_orders:
                if str(order.get("order_id") or "") == order_id or str(order.get("broker_order_id") or "") == order_id:
                    order["state"] = "CANCELLED"
        if "UPDATE core.core_order_quarantine" in str(sql):
            if "quarantine_id = $1" in str(sql):
                quarantine_id = str(args[0] if args else "")
                for row in self.quarantine_rows:
                    if str(row.get("quarantine_id") or "") == quarantine_id:
                        row["status"] = "resolved"
                if not self.quarantine_rows:
                    self.quarantine = False
            else:
                symbol = str(args[0] if args else "").upper()
                if not symbol or symbol == self.quarantine_symbol:
                    self.quarantine = False
        if "DELETE FROM core.manual_intervention_lock" in str(sql):
            symbol = str(args[0] if args else "").upper()
            account_id = str(args[2] if len(args) >= 3 else "")
            strategy_instance_id = str(args[3] if len(args) >= 4 else "")
            position_owner_key = str(args[4] if len(args) >= 5 else "")
            lot_id = str(args[5] if len(args) >= 6 else "")
            self.manual_locks = [
                lock
                for lock in self.manual_locks
                if not (
                    str(lock.get("symbol") or "").upper() == symbol
                    and str(lock.get("reason") or "") == "strategy_recovery_action_quarantine"
                    and self._manual_lock_matches_owner_or_legacy_empty(
                        lock,
                        account_id=account_id,
                        strategy_instance_id=strategy_instance_id,
                        position_owner_key=position_owner_key,
                        lot_id=lot_id,
                    )
                )
            ]
        return "UPDATE 1"

    @staticmethod
    def _manual_lock_matches_owner_or_legacy_empty(
        lock,
        *,
        account_id: str,
        strategy_instance_id: str,
        position_owner_key: str,
        lot_id: str,
    ) -> bool:
        context = lock.get("context") if isinstance(lock.get("context"), dict) else {}
        lock_account_id = str(lock.get("account_id") or context.get("account_id") or "")
        lock_instance_id = str(lock.get("strategy_instance_id") or context.get("strategy_instance_id") or "")
        lock_owner_key = str(context.get("position_owner_key") or "")
        lock_lot_id = str(context.get("position_lot_id") or context.get("lot_id") or "")
        return (
            (not account_id or lock_account_id == account_id)
            and (not strategy_instance_id or lock_instance_id == strategy_instance_id)
            and (not position_owner_key or lock_owner_key == position_owner_key)
            and (not lot_id or lock_lot_id == lot_id)
        )


class _FakePositionAdapter:
    account = "DU123"

    def __init__(
        self,
        *,
        symbol: str = "TQQQ",
        qty: float = 10.0,
        market_price: float = 100.0,
        open_orders=None,
        open_orders_error: Exception | None = None,
    ) -> None:
        self.symbol = symbol
        self.qty = qty
        self.market_price = market_price
        self._open_orders = list(open_orders or [])
        self._open_orders_error = open_orders_error
        self.cancel_calls = []

    def positions(self):
        return [
            type(
                "Position",
                (),
                {
                    "account": "DU123",
                    "contract": type("Contract", (), {"symbol": self.symbol, "secType": "STK"})(),
                    "position": self.qty,
                    "marketPrice": self.market_price,
                },
            )()
        ]

    async def open_orders(self, *, force_refresh: bool = False):
        if self._open_orders_error is not None:
            raise self._open_orders_error
        return list(self._open_orders)

    async def cancel(self, broker_order_id: int):
        self.cancel_calls.append(int(broker_order_id))
        self._open_orders = [
            order
            for order in self._open_orders
            if str(order.get("broker_order_id") or order.get("order_id") or "") != str(broker_order_id)
        ]
        return True


class _FakeRecoveryOrderManager:
    def __init__(self, *, success: bool = True, reason: str = "submitted") -> None:
        self.success = success
        self.reason = reason
        self.calls = []

    async def submit_flatten(self, result):
        self.calls.append(result)
        return SimpleNamespace(success=self.success, reason=self.reason, stage="fake_recovery")


@pytest.mark.asyncio
async def test_position_safety_recovery_flatten_carries_owner_lane_order_ref(tmp_path: Path) -> None:
    manager = _FakeRecoveryOrderManager()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=1.0),
        manual_intervention_lock_store=_FakeManualLockStore(),
        recovery_order_manager=manager,
        artifact_dir=tmp_path,
    )

    submitted = await supervisor._submit_position_recovery_flatten(
        symbol="NVDA",
        qty=1.0,
        contract_strategy_ids=("nvda_minute_flip_paper_001",),
        owner_lanes=(
            {
                "account_id": "DUO112752",
                "strategy_id": "nvda_minute_flip",
                "strategy_instance_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "lot_id": "lot-nvda-001",
                "qty": 1.0,
            },
        ),
        now_et=datetime.fromisoformat("2026-05-13T06:46:00-04:00"),
        reason="broker_position_without_protection_flatten_quarantine_or_manual_lock",
        recovery_incident_id="incident-1",
    )

    assert submitted is True
    assert manager.calls
    extras = manager.calls[0].metadata["envelope_extras"]
    assert extras["account_id"] == "DUO112752"
    assert extras["strategy_instance_id"] == "nvda_minute_flip_paper_001"
    assert extras["position_lot_id"] == "lot-nvda-001"
    assert extras["order_ref"].startswith("rflat:DUO112752|nvda_minute_flip_paper_001|NVDA|lot-nvda-001|")
    assert not extras["order_ref"].startswith("aux:NVDA:")


def _live_broker_truth_snapshot(**overrides):
    payload = {
        "snapshot_id": "snap-1",
        "artifact_type": "live_broker_truth_snapshot",
        "artifact_present": True,
        "generated_at": "2026-05-04T10:00:00-04:00",
        "source": "live_broker",
        "broker_truth_backed": True,
        "source_process_pid": 123,
        "account": "DU123",
        "account_invalid": False,
        "lookup_failed": False,
        "stale": False,
        "go_no_go": "go",
        "broker_position_count": 1,
        "broker_open_order_count": 0,
        "positions": [{"account": "DU123", "symbol": "TQQQ", "qty": 10.0, "market_ref_px": 100.0}],
        "open_orders": [],
    }
    payload.update(overrides)
    return payload


def test_position_safety_dependency_ok_with_fresh_broker_truth() -> None:
    result = validate_position_safety_broker_truth_dependency(
        _live_broker_truth_snapshot(),
        now=datetime.fromisoformat("2026-05-04T10:00:05-04:00"),
        ttl_sec=90.0,
        required=True,
    )

    assert result.state == "OK"
    assert result.ok is True
    assert result.lookup_failed is False
    assert result.cleanup_allowed is True
    assert result.dependency_state["broker_truth_snapshot_id"] == "snap-1"
    assert result.dependency_state["broker_truth_age_sec"] == 5.0


@pytest.mark.parametrize(
    ("snapshot", "state", "blocker"),
    [
        (None, "MISSING", "broker_truth_missing"),
        (_live_broker_truth_snapshot(stale=True), "STALE", "broker_truth_stale"),
        (
            _live_broker_truth_snapshot(source="db_projection_only", broker_truth_backed=False),
            "NOT_BROKER_BACKED",
            "broker_truth_not_broker_backed",
        ),
        (
            _live_broker_truth_snapshot(lookup_failed=True, go_no_go="no_go", error="manager_handle_unavailable"),
            "LOOKUP_FAILED",
            "broker_truth_lookup_failed",
        ),
    ],
)
def test_position_safety_dependency_blocks_bad_broker_truth(snapshot, state, blocker) -> None:
    result = validate_position_safety_broker_truth_dependency(
        snapshot,
        now=datetime.fromisoformat("2026-05-04T10:00:05-04:00"),
        ttl_sec=90.0,
        required=True,
    )

    assert result.state == state
    assert result.go_no_go == "no_go"
    assert result.lookup_failed is True
    assert result.blocker == blocker
    assert result.cleanup_allowed is False


@pytest.mark.asyncio
async def test_session_end_supervisor_creates_incident_for_no_overnight_position(tmp_path: Path) -> None:
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    supervisor = SessionEndSupervisor(
        ib_adapter=_FakePositionAdapter(symbol="TQQQ", qty=10.0),
        config={
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {
                        "extras": {
                            "production_manifest": {
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True}
                            },
                            "force_flatten_time": "15:59:00",
                        }
                    },
                }
            ]
        },
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        recovery_incident_service=incident_service,
        artifact_dir=tmp_path / "session_end",
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T16:01:00-04:00"))
    index = incident_service.write_index(now_et=datetime.fromisoformat("2026-05-04T16:01:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["violation_count"] == 1
    assert artifact["violations"][0]["recovery_incident_id"] == "recovery_incident:qqq_intraday_001:TQQQ"
    assert index["active_count"] == 1
    assert index["incidents"][0]["source"] == "session_end_supervisor"
    assert (tmp_path / "session_end/session_end_latest.json").exists()


@pytest.mark.asyncio
async def test_session_end_supervisor_skips_extended_equity_policy_after_rth_close(tmp_path: Path) -> None:
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    supervisor = SessionEndSupervisor(
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=1.0),
        config={
            "strategies": [
                {
                    "id": "nvda_minute_flip_paper_001",
                    "enabled": True,
                    "module": "strategies.stocks_kernel.strategy",
                    "class": "ComposableStockKernelStrategy",
                    "params": {
                        "extras": {
                            "account_id": "DUO112752",
                            "data_contract": {"symbols": ["NVDA"]},
                            "gate_overrides": {
                                "session_window": {
                                    "params": {"allowed_sessions": ["PRE", "POST", "OVERNIGHT"]}
                                }
                            },
                            "production_manifest": {
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True}
                            },
                            "force_flatten_time": "15:59:00",
                        }
                    },
                }
            ]
        },
        strategy_data_contracts=[
            type(
                "Contract",
                (),
                {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True},
            )()
        ],
        recovery_incident_service=incident_service,
        artifact_dir=tmp_path / "session_end",
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T18:22:00-04:00"))
    index = incident_service.write_index(now_et=datetime.fromisoformat("2026-05-13T18:22:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["policy_count"] == 0
    assert artifact["checked_count"] == 0
    assert artifact["violation_count"] == 0
    assert index["active_count"] == 0


@pytest.mark.asyncio
async def test_session_end_supervisor_resolves_source_incident_when_flat(tmp_path: Path) -> None:
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    now = datetime.fromisoformat("2026-05-04T16:01:00-04:00")
    incident_service.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="session_end_no_overnight_position_not_flat",
        now_et=now,
        source="session_end_supervisor",
    )
    supervisor = SessionEndSupervisor(
        ib_adapter=_FakePositionAdapter(symbol="TQQQ", qty=0.0),
        config={
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {
                        "extras": {
                            "production_manifest": {
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True}
                            },
                            "force_flatten_time": "15:59:00",
                        }
                    },
                }
            ]
        },
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        recovery_incident_service=incident_service,
        artifact_dir=tmp_path / "session_end",
    )

    artifact = await supervisor.run_once(now=now)
    index = incident_service.write_index(now_et=now)

    assert artifact["go_no_go"] == "go"
    assert artifact["violation_count"] == 0
    assert index["active_count"] == 0
    assert index["incidents"][0]["state"] == "verified_flat"


@pytest.mark.asyncio
async def test_session_end_supervisor_escalates_stale_violation_to_manual_required(tmp_path: Path) -> None:
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    first_seen = datetime.fromisoformat("2026-05-04T16:00:00-04:00")
    now = datetime.fromisoformat("2026-05-04T16:06:00-04:00")
    incident_service.upsert_active(
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        qty=10.0,
        reason="session_end_no_overnight_position_not_flat",
        now_et=first_seen,
        source="session_end_supervisor",
    )
    lock_store = _FakeManualLockStore()
    supervisor = SessionEndSupervisor(
        ib_adapter=_FakePositionAdapter(symbol="TQQQ", qty=10.0),
        config={
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {
                        "extras": {
                            "production_manifest": {
                                "eod_contract": {"no_overnight": True, "platform_eod_required": True}
                            },
                            "force_flatten_time": "15:59:00",
                        }
                    },
                }
            ]
        },
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        manual_intervention_lock_store=lock_store,
        recovery_incident_service=incident_service,
        artifact_dir=tmp_path / "session_end",
        manual_lock_dir=tmp_path / "manual_locks",
        manual_required_after_sec=300,
    )

    artifact = await supervisor.run_once(now=now)
    index = incident_service.write_index(now_et=now)

    assert artifact["go_no_go"] == "no_go"
    assert artifact["manual_required_count"] == 1
    assert artifact["violations"][0]["manual_required"] is True
    assert list(artifact["violations"][0]["manual_lock_ids"]) == [
        "session_end_manual_lock:2026-05-04:qqq_intraday_001:TQQQ"
    ]
    assert lock_store.calls[0]["reason"] == "session_end_manual_required"
    assert index["active_count"] == 0
    assert index["incidents"][0]["state"] == "manual_required_acknowledged"
    assert list((tmp_path / "manual_locks").glob("session_end_manual_lock_*.json"))


@pytest.mark.asyncio
async def test_position_safety_supervisor_locks_unclassified_broker_position(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter()
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["broker_position_count"] == 1
    assert lock_store.calls[0]["strategy_id"] == "qqq_intraday_001"
    assert lock_store.calls[0]["symbol"] == "TQQQ"
    assert lock_store.calls[0]["reason"] == "position_safety_manual_required"
    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert (tmp_path / "position_safety_latest.json").exists()


@pytest.mark.asyncio
async def test_position_safety_supervisor_writes_current_no_go_artifact_on_unhandled_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.fromisoformat("2026-05-04T10:00:00-04:00")
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=_FakePositionAdapter(),
        manual_intervention_lock_store=_FakeManualLockStore(),
        artifact_dir=tmp_path,
    )

    async def fail_once(*, now=None):
        raise RuntimeError("unexpected_position_safety_failure")

    monkeypatch.setattr(supervisor, "_run_once_locked", fail_once)

    with pytest.raises(RuntimeError, match="unexpected_position_safety_failure"):
        await supervisor.run_once(now=now)

    artifact = json.loads((tmp_path / "position_safety_latest.json").read_text(encoding="utf-8"))
    assert artifact["generated_at"] == now.isoformat()
    assert artifact["go_no_go"] == "no_go"
    assert artifact["lookup_failed"] is True
    assert artifact["blockers"] == ["position_safety_supervisor_exception"]
    assert artifact["error"] == "unexpected_position_safety_failure"


@pytest.mark.asyncio
async def test_position_safety_supervisor_allows_recent_protection_convergence_window(tmp_path: Path) -> None:
    now = datetime.fromisoformat("2026-05-04T10:00:00-04:00")
    adapter = _FakePositionAdapter()
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            protection_registry=[
                {
                    "protection_key": "DU123|qqq_intraday_001|TQQQ|lot-1|protective_stop|SELL|10|px",
                    "strategy_id": "qqq_intraday_001",
                    "strategy_instance_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "side": "SELL",
                    "desired_qty": 10,
                    "state": "SUBMITTED",
                    "updated_at": now,
                    "first_seen_at": now,
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
        protection_convergence_window_sec=60.0,
    )

    artifact = await supervisor.run_once(now=now)

    assert artifact["records"][0]["classified_as"] == "protection_converging"
    assert artifact["records"][0]["reason"] == "protection_lifecycle_converging"
    assert "unsafe_broker_position" not in artifact["blockers"]
    assert "active_manual_intervention_lock" not in artifact["blockers"]
    assert lock_store.calls == []


@pytest.mark.asyncio
async def test_position_safety_supervisor_locks_stale_protection_convergence(tmp_path: Path) -> None:
    now = datetime.fromisoformat("2026-05-04T10:00:00-04:00")
    stale = datetime.fromisoformat("2026-05-04T09:58:00-04:00")
    adapter = _FakePositionAdapter()
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            protection_registry=[
                {
                    "protection_key": "DU123|qqq_intraday_001|TQQQ|lot-1|protective_stop|SELL|10|px",
                    "strategy_id": "qqq_intraday_001",
                    "strategy_instance_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "side": "SELL",
                    "desired_qty": 10,
                    "state": "SUBMITTED",
                    "updated_at": stale,
                    "first_seen_at": stale,
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
        protection_convergence_window_sec=30.0,
    )

    artifact = await supervisor.run_once(now=now)

    assert artifact["go_no_go"] == "no_go"
    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_blocks_invalid_broker_account_scope(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    adapter.account = ""
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["broker_account_invalid"] is True
    assert artifact["live_broker_snapshot"]["go_no_go"] == "no_go"
    assert artifact["live_broker_snapshot"]["account_invalid"] is True


@pytest.mark.asyncio
async def test_position_safety_consumes_fresh_broker_truth_artifact(tmp_path: Path) -> None:
    class _DirectConnectAdapter:
        account = "DU123"

        def positions(self):
            raise AssertionError("position safety must not direct-read adapter positions")

        async def open_orders(self, *, force_refresh: bool = False):
            raise AssertionError("position safety must not direct-read adapter open orders")

    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=_DirectConnectAdapter(),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        live_broker_snapshot_provider=_live_broker_truth_snapshot(),
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["lookup_failed"] is False
    assert artifact["live_broker_snapshot"]["source"] == "live_broker"
    assert artifact["dependency_state"]["broker_truth_snapshot_id"] == "snap-1"
    assert artifact["dependency_state"]["broker_truth_backed"] is True
    assert artifact["broker_position_count"] == 1
    assert artifact["records"][0]["symbol"] == "TQQQ"
    live_artifact = tmp_path / "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json"
    assert live_artifact.exists()
    live_payload = json.loads(live_artifact.read_text(encoding="utf-8"))
    assert live_payload["artifact_type"] == "live_broker_truth_snapshot"
    assert live_payload["broker_truth_backed"] is True


@pytest.mark.asyncio
async def test_position_safety_fails_closed_when_broker_truth_unavailable(tmp_path: Path) -> None:
    class _DirectConnectAdapter:
        account = "DU123"

        def positions(self):
            raise AssertionError("position safety must not fallback to direct adapter positions")

        async def open_orders(self, *, force_refresh: bool = False):
            raise AssertionError("position safety must not fallback to direct adapter open orders")

    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=_DirectConnectAdapter(),
        manual_intervention_lock_store=_FakeManualLockStore(),
        live_broker_snapshot_provider=_live_broker_truth_snapshot(
            source="unavailable",
            broker_truth_backed=False,
            lookup_failed=True,
            go_no_go="no_go",
            error="manager_handle_unavailable",
            broker_position_count=0,
            positions=[],
        ),
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["lookup_failed"] is True
    assert artifact["error"] == "manager_handle_unavailable"
    assert artifact["live_broker_snapshot"]["lookup_failed"] is True
    assert "broker_truth_lookup_failed" in artifact["blockers"]
    assert artifact["core_position_residual_cleanup"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_position_safety_dependency_missing_blocks_and_cleanup_blocked(tmp_path: Path) -> None:
    class _DirectConnectAdapter:
        account = "DU123"

        def positions(self):
            raise AssertionError("missing dependency must not fallback to direct adapter positions")

        async def open_orders(self, *, force_refresh: bool = False):
            raise AssertionError("missing dependency must not fallback to direct adapter open orders")

    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=_DirectConnectAdapter(),
        manual_intervention_lock_store=_FakeManualLockStore(),
        live_broker_snapshot_provider={},
        require_broker_truth_dependency=True,
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["lookup_failed"] is True
    assert "broker_truth_missing" in artifact["blockers"]
    assert artifact["dependency_state"]["broker_truth_available"] is False
    assert artifact["core_position_residual_cleanup"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_position_safety_does_not_call_exec_adapter_connect_for_managed_adapter(tmp_path: Path, monkeypatch) -> None:
    class _ManagedAdapter:
        account = "DU123"
        _managed_by_connection_manager = True

        def positions(self):
            raise AssertionError("managed adapter positions would direct-connect")

        async def open_orders(self, *, force_refresh: bool = False):
            raise AssertionError("managed adapter open_orders would direct-connect")

    async def _manager_snapshot(**kwargs):
        return _live_broker_truth_snapshot(broker_position_count=0, positions=[])

    monkeypatch.setattr(
        "services.production.position_safety_supervisor.build_live_broker_truth_snapshot_from_manager",
        _manager_snapshot,
    )
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=_ManagedAdapter(),
        manual_intervention_lock_store=_FakeManualLockStore(),
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["lookup_failed"] is False
    assert artifact["live_broker_snapshot"]["source"] == "live_broker"


@pytest.mark.asyncio
async def test_position_safety_supervisor_accepts_active_protection_order(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "manual_flatten:abc",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "status": "Submitted",
            }
        ]
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 10,
                    "broker_order_id": "9001",
                    "note": '{"close_only":true,"flatten_action":true}',
                    "order_ref": "manual_flatten:abc",
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "go"


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_flat_protection_failed_lock(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    lock_store = _FakeManualLockStore(
        active_lock={
            "reason": "protection_failed",
            "context": {"reason": "protection_failed"},
        }
    )
    pg = _FakePositionPg(
        manual_locks=[{"strategy_id": "stocks_kernel_smh_001", "symbol": "SOXL", "reason": "protection_failed"}],
        core_positions={("stocks_kernel_smh_001", "SOXL"): 0.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "stocks_kernel_smh_001", "symbols": ("SOXL",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["flat_manual_lock_cleanup"]["cleared_count"] == 1
    assert lock_store.unlock_calls == [{"strategy_id": "stocks_kernel_smh_001", "symbol": "SOXL"}]


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_flat_manual_flatten_lock(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    lock_store = _FakeManualLockStore(
        active_lock={
            "reason": "manual_flatten",
            "context": {"trigger_id": "platform_eod_flatten:2026-05-04"},
        }
    )
    pg = _FakePositionPg(
        manual_locks=[{"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA", "reason": "manual_flatten"}],
        core_positions={("nvda_minute_flip_paper_001", "NVDA"): 0.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T16:10:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["flat_manual_lock_cleanup"]["cleared_count"] == 1
    assert lock_store.unlock_calls == [{"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA"}]


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_owner_scoped_flatten_lock_when_other_strategy_same_symbol_live(
    tmp_path: Path,
) -> None:
    adapter = _FakePositionAdapter(symbol="NVDA", qty=1.0)
    lock_store = _FakeManualLockStore(
        active_lock={
            "reason": "flatten_action",
            "context": {
                "account_id": "DU123",
                "close_only": True,
                "flatten_action": True,
                "position_lot_id": "lot-1",
                "position_owner_key": "nvda_minute_flip_paper_001:NVDA:lot-1",
                "strategy_instance_id": "nvda_minute_flip_paper_001",
            },
        }
    )
    pg = _FakePositionPg(
        manual_locks=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "reason": "flatten_action",
                "context": {
                    "account_id": "DU123",
                    "close_only": True,
                    "flatten_action": True,
                    "position_lot_id": "lot-1",
                    "position_owner_key": "nvda_minute_flip_paper_001:NVDA:lot-1",
                    "strategy_instance_id": "nvda_minute_flip_paper_001",
                },
            }
        ],
        core_positions={
            ("nvda_minute_flip_paper_001", "NVDA"): 0.0,
            ("nvda_minute_flip_paper_002", "NVDA"): 1.0,
        },
        ledger_positions={
            ("nvda_minute_flip_paper_001", "NVDA"): 0.0,
            ("nvda_minute_flip_paper_002", "NVDA"): 1.0,
        },
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})(),
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_002", "symbols": ("NVDA",), "production_blocking": True})(),
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T01:44:00-04:00"))

    assert artifact["flat_manual_lock_cleanup"]["cleared_count"] == 1
    assert artifact["flat_manual_lock_cleanup"]["items"][0]["scope"] == "owner_lane"
    assert lock_store.unlock_calls == [{"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA"}]


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_flat_position_mismatch_lock(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    lock_store = _FakeManualLockStore(
        active_lock={
            "reason": "position_mismatch_quarantine",
            "context": {"reason": "position_mismatch_quarantine"},
        }
    )
    pg = _FakePositionPg(
        manual_locks=[
            {"strategy_id": "qqq_intraday_option_v1_001", "symbol": "OPT:QQQ:20260505:682:C", "reason": "position_mismatch_quarantine"}
        ],
        core_positions={("qqq_intraday_option_v1_001", "OPT:QQQ:20260505:682:C"): 0.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_option_v1_001", "symbols": ("QQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["flat_manual_lock_cleanup"]["cleared_count"] == 1
    assert lock_store.unlock_calls == [{"strategy_id": "qqq_intraday_option_v1_001", "symbol": "OPT:QQQ:20260505:682:C"}]


@pytest.mark.asyncio
async def test_position_safety_supervisor_keeps_manual_lock_when_core_position_nonflat(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    lock_store = _FakeManualLockStore(
        active_lock={
            "reason": "protection_failed",
            "context": {"reason": "protection_failed"},
        }
    )
    pg = _FakePositionPg(
        manual_locks=[{"strategy_id": "stocks_kernel_smh_001", "symbol": "SOXL", "reason": "protection_failed"}],
        core_positions={("stocks_kernel_smh_001", "SOXL"): 70.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "stocks_kernel_smh_001", "symbols": ("SOXL",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["flat_manual_lock_cleanup"]["cleared_count"] == 0
    assert artifact["flat_manual_lock_cleanup"]["skipped_count"] == 1
    assert lock_store.unlock_calls == []


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_flat_core_position_residual_with_owner_lane_evidence(
    tmp_path: Path,
) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    lock_store = _FakeManualLockStore()
    pg = _FakePositionPg(
        core_positions={("stocks_kernel_smh_001", "SOXL"): 68.0},
        ledger_positions={("stocks_kernel_smh_001", "SOXL"): 0.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "stocks_kernel_smh_001", "symbols": ("SOXL",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["core_position_residual_cleanup"]["checked_count"] == 1
    assert artifact["core_position_residual_cleanup"]["cleared_count"] == 1
    assert artifact["core_position_residual_cleanup"]["skipped_count"] == 0
    item = artifact["core_position_residual_cleanup"]["items"][0]
    assert item["status"] == "cleared"
    assert item["reason"] == "owner_lane_ledger_flat_broker_flat_no_open_orders"
    assert pg.core_positions[("stocks_kernel_smh_001", "SOXL")] == 0.0
    assert any("UPDATE core.core_position" in sql for sql, _args in pg.executed)


@pytest.mark.asyncio
async def test_position_safety_supervisor_keeps_core_position_when_ledger_nonflat(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    pg = _FakePositionPg(
        core_positions={("stocks_kernel_smh_001", "SOXL"): 68.0},
        ledger_positions={("stocks_kernel_smh_001", "SOXL"): 68.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "stocks_kernel_smh_001", "symbols": ("SOXL",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["core_position_residual_cleanup"]["cleared_count"] == 0
    assert artifact["core_position_residual_cleanup"]["skipped_count"] == 1
    assert artifact["core_position_residual_cleanup"]["items"][0]["reason"] == "ledger_nonflat"
    assert pg.core_positions[("stocks_kernel_smh_001", "SOXL")] == 68.0


@pytest.mark.asyncio
async def test_position_safety_supervisor_keeps_core_position_when_nonterminal_order_exists(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    pg = _FakePositionPg(
        core_positions={("stocks_kernel_smh_001", "SOXL"): 68.0},
        ledger_positions={("stocks_kernel_smh_001", "SOXL"): 0.0},
        nonterminal_orders={("stocks_kernel_smh_001", "SOXL")},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "stocks_kernel_smh_001", "symbols": ("SOXL",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["core_position_residual_cleanup"]["cleared_count"] == 0
    assert artifact["core_position_residual_cleanup"]["skipped_count"] == 1
    assert artifact["core_position_residual_cleanup"]["items"][0]["reason"] == "nonterminal_order_present"
    assert pg.core_positions[("stocks_kernel_smh_001", "SOXL")] == 68.0


@pytest.mark.asyncio
async def test_position_safety_supervisor_terminalizes_absent_flat_close_order_before_core_cleanup(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(qty=0.0, open_orders=[])
    pg = _FakePositionPg(
        core_positions={("stocks_kernel_smh_001", "SOXL"): 68.0},
        ledger_positions={("stocks_kernel_smh_001", "SOXL"): 0.0},
        open_orders=[
            {
                "order_id": "order-1",
                "strategy_id": "stocks_kernel_smh_001",
                "symbol": "SOXL",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 68.0,
                "broker_order_id": "380",
                "order_ref": "stkv1:stocks_kernel_smh_001:SOXL:FLATTEN",
                "note": {"flatten_action": True, "close_only": True},
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "stocks_kernel_smh_001", "symbols": ("SOXL",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    cleanup = artifact["core_position_residual_cleanup"]["items"][0]["stale_close_order_cleanup"]
    assert cleanup["terminalized_count"] == 1
    assert artifact["core_position_residual_cleanup"]["cleared_count"] == 1
    assert artifact["core_position_residual_cleanup"]["skipped_count"] == 0
    assert artifact["core_position_residual_cleanup"]["items"][0]["reason"] == (
        "owner_lane_ledger_flat_broker_flat_no_open_orders"
    )
    assert pg.core_positions[("stocks_kernel_smh_001", "SOXL")] == 0.0
    assert any("UPDATE core.core_order" in sql for sql, _args in pg.executed)


@pytest.mark.asyncio
async def test_position_safety_supervisor_terminalizes_plain_reduce_only_close_order_and_resolves_flat_quarantine(
    tmp_path: Path,
) -> None:
    adapter = _FakePositionAdapter(qty=0.0, open_orders=[])
    symbol = "OPT:NVDA:20260506:207.5:P"
    pg = _FakePositionPg(
        quarantine=True,
        quarantine_symbol=symbol,
        quarantine_reason="trace_validation_failed:strategy_run_id,trigger_id,trace_id",
        core_positions={("nvda_option_churn_smoke_001", symbol): 1.0},
        ledger_positions={("nvda_option_churn_smoke_001", symbol): 0.0},
        open_orders=[
            {
                "order_id": "order-1",
                "strategy_id": "nvda_option_churn_smoke_001",
                "symbol": symbol,
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "637",
                "order_ref": "opt0dte_exit:nvda_option_churn_smoke_001:a0cf",
                "note": {},
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_option_churn_smoke_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-06T22:00:00-04:00"))

    cleanup = artifact["core_position_residual_cleanup"]["items"][0]["stale_close_order_cleanup"]
    assert cleanup["terminalized_count"] == 1
    assert cleanup["items"][0]["reason"] == "reduce_only_close_order_absent_after_broker_and_ledger_flat"
    assert artifact["core_position_residual_cleanup"]["items"][0]["reason"] == (
        "owner_lane_ledger_flat_broker_flat_no_open_orders"
    )
    assert artifact["flat_quarantine_cleanup"]["resolved_count"] == 1
    assert artifact["active_quarantine_count"] == 0
    assert artifact["go_no_go"] == "go"


@pytest.mark.asyncio
async def test_position_safety_supervisor_resolves_flat_option_quarantine_after_absent_order_terminalized(
    tmp_path: Path,
) -> None:
    adapter = _FakePositionAdapter(qty=0.0, open_orders=[])
    symbol = "OPT:QQQ:20260513:712:P"
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    incident_service.upsert_active(
        strategy_id="qqq_intraday_option_v1_001",
        symbol=symbol,
        qty=2.0,
        reason="broker_only_position_requires_manual_intervention",
        now_et=datetime.fromisoformat("2026-05-13T12:21:12-04:00"),
        source="runtime_reconciler_position_mismatch",
        active_quarantine=True,
        protected_or_flattening=False,
    )
    pg = _FakePositionPg(
        quarantine=True,
        quarantine_symbol=symbol,
        quarantine_reason="scoped_tradability_denied:position_safety_no_go",
        manual_locks=[
            {
                "strategy_id": "qqq_intraday_option_v1_001",
                "symbol": symbol,
                "reason": "position_mismatch_quarantine",
                "context": {"source": "runtime_reconciler_position_mismatch"},
            }
        ],
        ledger_positions={("DU123", "qqq_intraday_option_v1_001", "qqq_intraday_option_v1_001", symbol, "lot-1"): 0.0},
        open_orders=[
            {
                "order_id": "stale-local-order",
                "strategy_id": "qqq_intraday_option_v1_001",
                "symbol": symbol,
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 2.0,
                "broker_order_id": "996",
                "order_ref": "manual_flatten:DU123|qqq_intraday_option_v1_001|OPT:QQQ:20260513:712:P|",
                "note": {"flow_type": "STRATEGY_FLATTEN"},
            }
        ],
    )
    lock_store = _FakeManualLockStore(
        active_locks=[
            {
                "strategy_id": "qqq_intraday_option_v1_001",
                "symbol": symbol,
                "reason": "position_mismatch_quarantine",
                "context": {"source": "runtime_reconciler_position_mismatch"},
            }
        ]
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type(
                "Contract",
                (),
                {"strategy_id": "qqq_intraday_option_v1_001", "symbols": ("QQQ",), "production_blocking": True},
            )()
        ],
        recovery_incident_service=incident_service,
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T17:35:08-04:00"))

    cleanup_item = artifact["flat_quarantine_cleanup"]["items"][0]
    assert cleanup_item["status"] == "resolved"
    assert cleanup_item["stale_order_cleanup"]["terminalized_count"] == 1
    assert cleanup_item["stale_order_cleanup"]["items"][0]["reason"] == (
        "nonterminal_order_absent_after_broker_core_ledger_flat"
    )
    assert pg.open_orders[0]["state"] == "CANCELLED"
    assert artifact["flat_quarantine_cleanup"]["resolved_count"] == 1
    assert artifact["flat_manual_lock_cleanup"]["lookup_failed"] is False
    assert artifact["active_quarantine_count"] == 0
    assert artifact["recovery_incidents"]["active_count"] == 0
    assert artifact["go_no_go"] == "go"


@pytest.mark.asyncio
async def test_position_safety_supervisor_keeps_flat_quarantine_when_db_order_still_live_at_broker(
    tmp_path: Path,
) -> None:
    symbol = "OPT:QQQ:20260513:712:P"
    broker_order = {
        "broker_order_id": "996",
        "order_id": "996",
        "order_ref": "manual_flatten:DU123|qqq_intraday_option_v1_001|OPT:QQQ:20260513:712:P|",
        "symbol": symbol,
        "side": "SELL",
        "qty": 2.0,
        "status": "Submitted",
    }
    adapter = _FakePositionAdapter(qty=0.0, open_orders=[broker_order])
    pg = _FakePositionPg(
        quarantine=True,
        quarantine_symbol=symbol,
        quarantine_reason="scoped_tradability_denied:position_safety_no_go",
        open_orders=[
            {
                "order_id": "stale-local-order",
                "strategy_id": "qqq_intraday_option_v1_001",
                "symbol": symbol,
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 2.0,
                "broker_order_id": "996",
                "order_ref": broker_order["order_ref"],
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type(
                "Contract",
                (),
                {"strategy_id": "qqq_intraday_option_v1_001", "symbols": ("QQQ",), "production_blocking": True},
            )()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T17:35:08-04:00"))

    cleanup_item = artifact["flat_quarantine_cleanup"]["items"][0]
    assert cleanup_item["status"] == "skipped"
    assert cleanup_item["skip_reason"] == "broker_order_still_open"
    assert pg.open_orders[0]["state"] == "SUBMITTED"
    assert artifact["active_quarantine_count"] == 1
    assert artifact["go_no_go"] == "no_go"


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_flat_option_core_position_for_production_strategy(
    tmp_path: Path,
) -> None:
    adapter = _FakePositionAdapter(qty=0.0)
    symbol = "OPT:QQQ:20260501:675:C"
    pg = _FakePositionPg(
        core_positions={("qqq_intraday_option_v1_001", symbol): 2.0},
        ledger_positions={("qqq_intraday_option_v1_001", symbol): 0.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type(
                "Contract",
                (),
                {"strategy_id": "qqq_intraday_option_v1_001", "symbols": ("QQQ",), "production_blocking": True},
            )()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["core_position_residual_cleanup"]["checked_count"] == 1
    assert artifact["core_position_residual_cleanup"]["cleared_count"] == 1
    assert artifact["core_position_residual_cleanup"]["skipped_count"] == 0
    assert artifact["core_position_residual_cleanup"]["items"][0]["status"] == "cleared"
    assert pg.core_positions[("qqq_intraday_option_v1_001", symbol)] == 0.0


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_auto_lock_when_protection_arrives(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "sl:DU123|qqq_intraday_001|TQQQ|",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "status": "Submitted",
                "order_type": "STP",
                "aux_price": 95.0,
            }
        ]
    )
    lock_store = _FakeManualLockStore(
        active_lock={
            "reason": "position_safety_manual_required",
            "context": {
                "lock_id": "position_safety_manual_lock:2026-05-04:qqq_intraday_001:TQQQ",
                "reason": "broker_position_without_protection_flatten_quarantine_or_manual_lock",
            },
        }
    )
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 10,
                    "broker_order_id": "9001",
                    "note": '{"protective_stop":true}',
                    "order_ref": "sl:DU123|qqq_intraday_001|TQQQ|",
                    "order_type": "STP",
                    "stop_px": 95.0,
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["records"][0]["classified_as"] == "protected_or_flattening"
    assert artifact["records"][0]["active_manual_lock_count"] == 0
    assert lock_store.unlock_calls == [{"strategy_id": "qqq_intraday_001", "symbol": "TQQQ"}]
    assert lock_store.calls == []


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_plain_working_entry_as_safety(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter()
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "BUY",
                    "note": '{"entry":true}',
                    "order_ref": "entry:abc",
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 0
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_wrong_side_safety_order(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "manual_flatten:abc",
                "symbol": "TQQQ",
                "side": "BUY",
                "qty": 10,
                "status": "Submitted",
            }
        ]
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "BUY",
                    "qty": 10,
                    "broker_order_id": "9001",
                    "note": '{"close_only":true,"flatten_action":true}',
                    "order_ref": "manual_flatten:abc",
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 0
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_no_go_for_live_safety_order_that_adds_to_position(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=1.0,
        market_price=224.0,
        open_orders=[
            {
                "broker_order_id": "580",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "symbol": "NVDA",
                "side": "BUY",
                "qty": 1,
                "status": "PreSubmitted",
            }
        ],
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["unsafe_live_order_count"] == 1
    assert artifact["unsafe_live_orders"][0]["expected_side"] == "SELL"
    assert artifact["unsafe_live_orders"][0]["side"] == "BUY"


@pytest.mark.asyncio
async def test_position_safety_supervisor_flags_safety_order_above_owner_lane_qty(tmp_path: Path) -> None:
    protection_key = "DUO112752|nvda_minute_flip_paper_001|NVDA|lot-nvda-1|stop_loss|SELL|1|band:22148"
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=2.0,
        market_price=224.0,
        open_orders=[
            {
                "broker_order_id": "580",
                "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 2,
                "status": "PreSubmitted",
            }
        ],
    )
    pg = _FakePositionPg(
        open_orders=[
            {
                "broker_order_id": "580",
                "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                "strategy_id": "nvda_minute_flip_paper_001",
                "strategy_instance_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 2,
                "state": "SUBMITTED",
                "note": json.dumps(
                    {
                        "account_id": "DUO112752",
                        "strategy_instance_id": "nvda_minute_flip_paper_001",
                        "position_lot_id": "lot-nvda-1",
                        "protection_key": protection_key,
                    }
                ),
            }
        ],
        ledger_positions={("nvda_minute_flip_paper_001", "NVDA", "lot-nvda-1"): 1.0},
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["unsafe_live_order_count"] == 1
    assert artifact["unsafe_live_orders"][0]["qty"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_position_safety_supervisor_cancels_unsafe_live_safety_order_when_protected(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=1.0,
        market_price=224.0,
        open_orders=[
            {
                "broker_order_id": "580",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "symbol": "NVDA",
                "side": "BUY",
                "qty": 1,
                "status": "PreSubmitted",
                "order_type": "STP",
            },
            {
                "broker_order_id": "582",
                "order_ref": "nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1,
                "status": "Submitted",
                "order_type": "LMT",
                "limit_px": 223.5,
            },
        ],
    )
    pg = _FakePositionPg(
        open_orders=[
            {
                "order_id": "bad-buy-stop",
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "BUY",
                "qty": 1,
                "order_type": "STP",
                "broker_order_id": "580",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "note": '{"close_only":true,"protective_stop":true}',
            },
            {
                "order_id": "valid-flatten",
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1,
                "order_type": "LMT",
                "broker_order_id": "582",
                "order_ref": "nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA",
                "note": '{"close_only":true,"flatten_action":true}',
                "limit_px": 223.5,
            },
        ]
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
        flat_live_safety_order_auto_cancel_enabled=True,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert adapter.cancel_calls == [580]
    assert artifact["go_no_go"] == "go"
    assert artifact["unsafe_live_order_count"] == 0
    assert artifact["unsafe_live_order_cleanup"]["cancelled_count"] == 1
    assert artifact["records"][0]["protected_or_flattening"] is True
    assert any("UPDATE core.core_order" in sql for sql, _args in pg.executed)


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_unrelated_strategy_safety_order(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "manual_flatten:abc",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "status": "Submitted",
            }
        ]
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "other_strategy",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 10,
                    "broker_order_id": "9001",
                    "note": '{"close_only":true,"flatten_action":true}',
                    "order_ref": "manual_flatten:abc",
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 0
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_partial_safety_coverage(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "manual_flatten:abc",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 4,
                "status": "Submitted",
            }
        ]
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 4,
                    "broker_order_id": "9001",
                    "note": '{"close_only":true,"flatten_action":true}',
                    "order_ref": "manual_flatten:abc",
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 1
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_stale_db_safety_order_missing_at_broker(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(open_orders=[])
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 10,
                    "broker_order_id": "9001",
                    "note": '{"close_only":true,"flatten_action":true}',
                    "order_ref": "manual_flatten:abc",
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 0
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_bad_price_safety_order(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=1.0,
        market_price=198.0,
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "sl:nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1,
                "status": "Submitted",
                "order_type": "STP",
                "stop_px": 1361.11,
            }
        ],
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 1,
                    "broker_order_id": "9001",
                    "note": '{"protection":true}',
                    "order_ref": "sl:nvda_minute_flip_paper_001",
                    "order_type": "STP",
                    "stop_px": 1361.11,
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type(
                "Contract",
                (),
                {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True},
            )()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 0
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_unknown_account_safety_order(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=1.0,
        market_price=198.0,
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "sl:UNKNOWN_ACCOUNT|nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1,
                "status": "Submitted",
                "order_type": "STP",
                "stop_px": 194.43,
            }
        ],
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 1,
                    "broker_order_id": "9001",
                    "note": '{"protection_key":"UNKNOWN_ACCOUNT|nvda_minute_flip_paper_001|NVDA"}',
                    "order_ref": "sl:UNKNOWN_ACCOUNT|nvda_minute_flip_paper_001",
                    "order_type": "STP",
                    "stop_px": 194.43,
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type(
                "Contract",
                (),
                {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True},
            )()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 0
    assert lock_store.calls


@pytest.mark.asyncio
async def test_position_safety_supervisor_no_go_when_active_quarantine_exists(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "manual_flatten:abc",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "status": "Submitted",
            }
        ]
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            quarantine=True,
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 10,
                    "broker_order_id": "9001",
                    "note": '{"close_only":true,"flatten_action":true}',
                    "order_ref": "manual_flatten:abc",
                }
            ],
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["active_quarantine_count"] == 1
    assert artifact["quarantined_manual_required_count"] == 0
    assert artifact["records"][0]["classified_as"] == "quarantined"


@pytest.mark.asyncio
async def test_position_safety_supervisor_resolves_synthetic_quarantine_when_owner_lane_protected(
    tmp_path: Path,
) -> None:
    broker_order = {
        "broker_order_id": "952",
        "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "PreSubmitted",
        "order_type": "STP",
        "stop_px": 220.0,
    }
    pg = _FakePositionPg(
        quarantine=True,
        quarantine_symbol="NVDA",
        quarantine_reason="recovery_action_not_executable",
        quarantine_note='{"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_002","position_owner_key":"nvda_minute_flip_paper_002:nvda_minute_flip_paper_002:NVDA:lot-002","lot_id":"lot-002"}',
        core_positions={("nvda_minute_flip_paper_002", "NVDA"): 1.0},
        ledger_positions={
            (
                "DU123",
                "nvda_minute_flip_paper_002",
                "nvda_minute_flip_paper_002",
                "NVDA",
                "lot-002",
                "nvda_minute_flip_paper_002:nvda_minute_flip_paper_002:NVDA:lot-002",
            ): 1.0
        },
        open_orders=[
            {
                "strategy_id": "nvda_minute_flip_paper_002",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "952",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_002"}',
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=_FakePositionAdapter(
            symbol="NVDA",
            qty=1.0,
            market_price=224.1,
            open_orders=[broker_order],
        ),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_002", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T02:05:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["active_quarantine_count"] == 0
    assert artifact["protected_quarantine_cleanup"]["resolved_count"] == 1
    assert artifact["protected_quarantine_cleanup"]["items"][0]["owner_lane"]["strategy_id"] == "nvda_minute_flip_paper_002"
    assert artifact["records"][0]["classified_as"] == "protected_or_flattening"
    assert pg.quarantine is False


@pytest.mark.asyncio
async def test_position_safety_supervisor_resolves_owner_identity_denied_quarantine_when_owner_lane_protected(
    tmp_path: Path,
) -> None:
    broker_order = {
        "broker_order_id": "1534",
        "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "PreSubmitted",
        "order_type": "STP",
        "stop_px": 220.0,
    }
    pg = _FakePositionPg(
        quarantine=True,
        quarantine_symbol="NVDA",
        quarantine_reason="scoped_tradability_denied:owner_identity_incomplete",
        quarantine_note='{"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001","position_owner_key":"nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001","lot_id":"lot-001"}',
        manual_locks=[
            {
                "strategy_id": "nvda_minute_flip_paper_002",
                "symbol": "NVDA",
                "reason": "strategy_recovery_action_quarantine",
                "context": {
                    "reason": "scoped_tradability_denied:owner_identity_incomplete",
                    "action_type": "flatten",
                    "account_id": "DU123",
                    "strategy_instance_id": "nvda_minute_flip_paper_001",
                    "position_owner_key": "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
                    "lot_id": "lot-001",
                },
            }
        ],
        core_positions={("nvda_minute_flip_paper_001", "NVDA"): 1.0},
        ledger_positions={
            (
                "DU123",
                "nvda_minute_flip_paper_001",
                "nvda_minute_flip_paper_001",
                "NVDA",
                "lot-001",
                "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
            ): 1.0
        },
        open_orders=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "1534",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001"}',
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=_FakePositionAdapter(
            symbol="NVDA",
            qty=1.0,
            market_price=224.1,
            open_orders=[broker_order],
        ),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T18:40:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["active_quarantine_count"] == 0
    assert artifact["active_manual_lock_count"] == 0
    assert artifact["protected_quarantine_cleanup"]["resolved_count"] == 1
    assert artifact["protected_quarantine_cleanup"]["items"][0]["owner_lane"]["strategy_id"] == "nvda_minute_flip_paper_001"
    assert pg.quarantine is False
    assert pg.manual_locks == []


@pytest.mark.asyncio
async def test_position_safety_supervisor_resolves_only_matching_protected_quarantine_row(
    tmp_path: Path,
) -> None:
    broker_order = {
        "broker_order_id": "1534",
        "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "PreSubmitted",
        "order_type": "STP",
        "stop_px": 220.0,
    }
    pg = _FakePositionPg(
        quarantine_rows=[
            {
                "quarantine_id": "quarantine-match",
                "symbol": "NVDA",
                "reason": "scoped_tradability_denied:owner_identity_incomplete",
                "status": "active",
                "note": '{"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001","position_owner_key":"nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001","lot_id":"lot-001"}',
            },
            {
                "quarantine_id": "quarantine-other-owner",
                "symbol": "NVDA",
                "reason": "scoped_tradability_denied:owner_identity_incomplete",
                "status": "active",
                "note": '{"strategy_instance_id":"nvda_minute_flip_paper_002","lot_id":"lot-002"}',
            },
        ],
        core_positions={("nvda_minute_flip_paper_001", "NVDA"): 1.0},
        ledger_positions={
            (
                "DU123",
                "nvda_minute_flip_paper_001",
                "nvda_minute_flip_paper_001",
                "NVDA",
                "lot-001",
                "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
            ): 1.0
        },
        open_orders=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "1534",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001"}',
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=1.0, market_price=224.1, open_orders=[broker_order]),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    result = await supervisor._resolve_protected_live_quarantines_after_owner_lane_verified(
        broker_positions=[{"symbol": "NVDA", "qty": 1.0, "market_ref_px": 224.1}],
        broker_open_orders=[broker_order],
        now_et=datetime.fromisoformat("2026-05-13T18:40:00-04:00"),
    )

    assert result["resolved_count"] == 1
    assert result["skipped_count"] == 1
    statuses = {row["quarantine_id"]: row["status"] for row in pg.quarantine_rows}
    assert statuses["quarantine-match"] == "resolved"
    assert statuses["quarantine-other-owner"] == "active"


@pytest.mark.asyncio
async def test_position_safety_supervisor_does_not_resolve_unscoped_protected_quarantine_by_default(
    tmp_path: Path,
) -> None:
    broker_order = {
        "broker_order_id": "1534",
        "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "PreSubmitted",
        "order_type": "STP",
        "stop_px": 220.0,
    }
    pg = _FakePositionPg(
        quarantine_rows=[
            {
                "quarantine_id": "quarantine-unscoped",
                "symbol": "NVDA",
                "reason": "scoped_tradability_denied:owner_identity_incomplete",
                "status": "active",
                "note": "{}",
            },
        ],
        core_positions={("nvda_minute_flip_paper_001", "NVDA"): 1.0},
        ledger_positions={
            (
                "DU123",
                "nvda_minute_flip_paper_001",
                "nvda_minute_flip_paper_001",
                "NVDA",
                "lot-001",
                "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
            ): 1.0
        },
        open_orders=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "1534",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001"}',
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=1.0, market_price=224.1, open_orders=[broker_order]),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    result = await supervisor._resolve_protected_live_quarantines_after_owner_lane_verified(
        broker_positions=[{"symbol": "NVDA", "qty": 1.0, "market_ref_px": 224.1}],
        broker_open_orders=[broker_order],
        now_et=datetime.fromisoformat("2026-05-13T18:40:00-04:00"),
    )

    assert result["resolved_count"] == 0
    assert result["skipped_count"] == 1
    assert result["items"][0]["skip_reason"] == "quarantine_owner_scope_mismatch"
    assert pg.quarantine_rows[0]["status"] == "active"


@pytest.mark.asyncio
async def test_position_safety_supervisor_resolves_db_persist_quarantine_when_owner_lane_protected(
    tmp_path: Path,
) -> None:
    broker_order = {
        "broker_order_id": "994",
        "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "PreSubmitted",
        "order_type": "STP",
        "stop_px": 220.0,
    }
    pg = _FakePositionPg(
        quarantine=True,
        quarantine_symbol="NVDA",
        quarantine_reason="db_persist_submitted_failed",
        quarantine_note='{"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001","position_owner_key":"nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001","lot_id":"lot-001"}',
        manual_locks=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "reason": "strategy_recovery_action_quarantine",
                "context": {
                    "reason": "db_persist_submitted_failed",
                    "action_type": "flatten",
                    "account_id": "DU123",
                    "strategy_instance_id": "nvda_minute_flip_paper_001",
                    "position_owner_key": "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
                    "lot_id": "lot-001",
                },
            }
        ],
        core_positions={("nvda_minute_flip_paper_001", "NVDA"): 1.0},
        ledger_positions={
            (
                "DU123",
                "nvda_minute_flip_paper_001",
                "nvda_minute_flip_paper_001",
                "NVDA",
                "lot-001",
                "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
            ): 1.0
        },
        open_orders=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "994",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001"}',
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=_FakePositionAdapter(
            symbol="NVDA",
            qty=1.0,
            market_price=224.1,
            open_orders=[broker_order],
        ),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T02:49:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["active_quarantine_count"] == 0
    assert artifact["active_manual_lock_count"] == 0
    assert artifact["protected_quarantine_cleanup"]["resolved_count"] == 1
    assert artifact["protected_quarantine_cleanup"]["items"][0]["owner_lane"]["strategy_id"] == "nvda_minute_flip_paper_001"
    assert pg.quarantine is False
    assert pg.manual_locks == []


@pytest.mark.asyncio
async def test_position_safety_supervisor_clears_stale_synthetic_recovery_lock_when_protected(
    tmp_path: Path,
) -> None:
    broker_order = {
        "broker_order_id": "994",
        "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "PreSubmitted",
        "order_type": "STP",
        "stop_px": 220.0,
        "account": "DU123",
    }
    lock_store = _FakeManualLockStore(
        active_lock={
            "strategy_id": "nvda_minute_flip_paper_001",
            "symbol": "NVDA",
            "reason": "strategy_recovery_action_quarantine",
            "context": {
                "reason": "db_persist_submitted_failed",
                "action_type": "flatten",
                "account_id": "DU123",
                "strategy_instance_id": "nvda_minute_flip_paper_001",
                "position_owner_key": "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
                "lot_id": "lot-001",
            },
        }
    )
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            quarantine=False,
            core_positions={("nvda_minute_flip_paper_001", "NVDA"): 1.0},
            ledger_positions={
                (
                    "DU123",
                    "nvda_minute_flip_paper_001",
                    "nvda_minute_flip_paper_001",
                    "NVDA",
                    "lot-001",
                    "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
                ): 1.0
            },
            open_orders=[
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 1.0,
                    "broker_order_id": "994",
                    "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                    "order_type": "STP",
                    "stop_px": 220.0,
                    "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001"}',
                }
            ],
        ),
        ib_adapter=_FakePositionAdapter(
            symbol="NVDA",
            qty=1.0,
            market_price=224.1,
            open_orders=[broker_order],
        ),
        manual_intervention_lock_store=lock_store,
        live_broker_snapshot_provider={
            "artifact_type": "live_broker_truth_snapshot",
            "generated_at": "2026-05-13T07:08:00-04:00",
            "broker_truth_backed": True,
            "source": "live_broker",
            "account": "DU123",
            "go_no_go": "go",
            "positions": [{"account": "DU123", "symbol": "NVDA", "qty": 1.0, "market_ref_px": 224.1}],
            "open_orders": [broker_order],
        },
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T07:08:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["blockers"] == []
    assert artifact["active_manual_lock_count"] == 0
    assert artifact["records"][0]["classified_as"] == "protected_or_flattening"
    assert artifact["records"][0]["active_manual_lock_count"] == 0
    assert lock_store.unlock_calls == [{"strategy_id": "nvda_minute_flip_paper_001", "symbol": "NVDA"}]


@pytest.mark.asyncio
async def test_position_safety_supervisor_keeps_synthetic_quarantine_when_owner_lane_ambiguous(
    tmp_path: Path,
) -> None:
    broker_order = {
        "broker_order_id": "952",
        "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "PreSubmitted",
        "order_type": "STP",
        "stop_px": 220.0,
    }
    pg = _FakePositionPg(
        quarantine=True,
        quarantine_symbol="NVDA",
        quarantine_reason="recovery_action_not_executable",
        core_positions={
            ("nvda_minute_flip_paper_001", "NVDA"): 1.0,
            ("nvda_minute_flip_paper_002", "NVDA"): 1.0,
        },
        ledger_positions={
            ("nvda_minute_flip_paper_001", "NVDA"): 1.0,
            ("nvda_minute_flip_paper_002", "NVDA"): 1.0,
        },
        open_orders=[
            {
                "strategy_id": "nvda_minute_flip_paper_002",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "952",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true}',
            }
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=1.0, market_price=224.1, open_orders=[broker_order]),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})(),
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_002", "symbols": ("NVDA",), "production_blocking": True})(),
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T02:05:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["active_quarantine_count"] == 1
    assert artifact["protected_quarantine_cleanup"]["resolved_count"] == 0
    assert artifact["protected_quarantine_cleanup"]["items"][0]["skip_reason"] == "owner_lane_not_verified"
    assert pg.quarantine is True


@pytest.mark.asyncio
async def test_position_safety_supervisor_ignores_stale_adopted_lane_when_owner_lanes_cover_broker_qty(
    tmp_path: Path,
) -> None:
    broker_orders = [
        {
            "broker_order_id": "1570",
            "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
            "symbol": "NVDA",
            "side": "SELL",
            "qty": 1.0,
            "status": "PreSubmitted",
            "order_type": "STP",
            "stop_px": 220.0,
        },
        {
            "broker_order_id": "1569",
            "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
            "symbol": "NVDA",
            "side": "SELL",
            "qty": 1.0,
            "status": "PreSubmitted",
            "order_type": "STP",
            "stop_px": 220.0,
        },
    ]
    pg = _FakePositionPg(
        quarantine_rows=[
            {
                "quarantine_id": "quarantine-aggregate-synthetic",
                "symbol": "NVDA",
                "reason": "recovery_action_not_executable",
                "status": "active",
                "note": '{"strategy_id":"","extras":{"ledger_qty":0.0,"owner_lane_qty":0.0}}',
            }
        ],
        core_positions={
            ("nvda_minute_flip_paper_001", "NVDA"): 1.0,
            ("nvda_minute_flip_paper_002", "NVDA"): 1.0,
        },
        ledger_positions={
            ("DU123", "adopted_reconciler", "", "NVDA", "", ""): -2.0,
            (
                "DU123",
                "nvda_minute_flip_paper_001",
                "nvda_minute_flip_paper_001",
                "NVDA",
                "lot-001",
                "nvda_minute_flip_paper_001:nvda_minute_flip_paper_001:NVDA:lot-001",
            ): 1.0,
            (
                "DU123",
                "nvda_minute_flip_paper_002",
                "nvda_minute_flip_paper_002",
                "NVDA",
                "lot-002",
                "nvda_minute_flip_paper_002:nvda_minute_flip_paper_002:NVDA:lot-002",
            ): 1.0,
        },
        open_orders=[
            {
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "1570",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_001|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_001"}',
            },
            {
                "strategy_id": "nvda_minute_flip_paper_002",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1.0,
                "broker_order_id": "1569",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
                "order_type": "STP",
                "stop_px": 220.0,
                "note": '{"protective_stop":true,"close_only":true,"account_id":"DU123","strategy_instance_id":"nvda_minute_flip_paper_002"}',
            },
        ],
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=2.0, market_price=224.1, open_orders=broker_orders),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})(),
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_002", "symbols": ("NVDA",), "production_blocking": True})(),
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T20:55:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["active_quarantine_count"] == 0
    assert artifact["protected_quarantine_cleanup"]["resolved_count"] == 1
    assert artifact["records"][0]["classified_as"] == "protected_or_flattening"
    assert artifact["records"][0]["contract_strategy_ids"] == (
        "nvda_minute_flip_paper_001",
        "nvda_minute_flip_paper_002",
    )
    assert pg.quarantine_rows[0]["status"] == "resolved"


@pytest.mark.asyncio
async def test_position_safety_supervisor_locks_unprotected_quarantined_position(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter()
    lock_store = _FakeManualLockStore()
    incident_service = RecoveryIncidentService(artifact_dir=tmp_path / "recovery_incidents")
    incident_service.mark_awaiting_market_reopen(
        strategy_id="runtime_reconciler",
        symbol="TQQQ",
        now_et=datetime.fromisoformat("2026-05-04T09:59:00-04:00"),
        qty=10.0,
        reason="manual_flatten_awaiting_market_reopen",
        not_before=datetime.fromisoformat("2026-05-04T10:00:00-04:00"),
        trigger_id="platform_eod_flatten:2026-05-04",
        side="SELL",
    )
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(quarantine=True),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        recovery_incident_service=incident_service,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["active_quarantine_count"] == 1
    assert artifact["quarantined_manual_required_count"] == 1
    record = artifact["records"][0]
    assert record["classified_as"] == "quarantined"
    assert record["active_manual_lock_count"] == 1
    assert record["reason"] == "active_quarantine_nonzero_position_requires_flatten_or_policy_repair"
    assert artifact["quarantined_manual_required"][0]["symbol"] == "TQQQ"
    assert lock_store.calls[0]["strategy_id"] == "qqq_intraday_001"
    assert lock_store.calls[0]["reason"] == "position_safety_manual_required"
    assert (
        lock_store.calls[0]["context"]["reason"]
        == "active_quarantine_nonzero_position_requires_flatten_or_policy_repair"
    )
    assert lock_store.calls[0]["context"]["awaiting_market_reopen"] is True
    assert lock_store.calls[0]["context"]["awaiting_market_reopen_not_before"] == "2026-05-04T10:00:00-04:00"


@pytest.mark.asyncio
async def test_position_safety_supervisor_submits_recovery_flatten_before_manual_lock(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(symbol="NVDA", qty=1.0)
    lock_store = _FakeManualLockStore()
    recovery_manager = _FakeRecoveryOrderManager(success=True)
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(quarantine=True),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        recovery_order_manager=recovery_manager,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["records"][0]["classified_as"] == "recovery_submitted"
    assert artifact["records"][0]["recovery_submitted"] is True
    assert artifact["recovery_submitted_count"] == 1
    assert lock_store.calls == []
    assert len(recovery_manager.calls) == 1
    call = recovery_manager.calls[0]
    assert call.plan.side == "SELL"
    assert call.plan.qty == 1.0
    extras = call.metadata["envelope_extras"]
    assert extras["position_safety_recovery"] is True
    assert extras["broker_truth_recovery"] is True
    assert extras["position_qty_hint"] == 1.0


@pytest.mark.asyncio
async def test_position_safety_supervisor_scopes_flatten_action_lock_while_protected(tmp_path: Path) -> None:
    broker_order = {
        "broker_order_id": "932",
        "order_id": "932",
        "order_ref": "nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1.0,
        "status": "Submitted",
        "order_type": "LMT",
        "limit_px": 223.54,
        "account": "DU123",
    }
    lock_store = _FakeManualLockStore(
        active_lock={
            "reason": "flatten_action",
            "context": {
                "account_id": "DU123",
                "strategy_instance_id": "nvda_minute_flip_paper_001",
                "close_only": True,
                "flatten_action": True,
                "position_lot_id": "lot-nvda-1",
                "position_owner_key": "nvda_minute_flip_paper_001:NVDA:lot-nvda-1",
            },
        }
    )
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 1.0,
                    "broker_order_id": "932",
                    "order_ref": "nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA",
                    "note": '{"flatten_action":true,"close_only":true,"account_id":"DU123"}',
                    "limit_px": 223.54,
                }
            ]
        ),
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=1.0, market_price=223.54, open_orders=[broker_order]),
        manual_intervention_lock_store=lock_store,
        live_broker_snapshot_provider={
            "artifact_type": "live_broker_truth_snapshot",
            "generated_at": "2026-05-13T01:07:00-04:00",
            "broker_truth_backed": True,
            "source": "live_broker",
            "account": "DU123",
            "go_no_go": "go",
            "positions": [{"account": "DU123", "symbol": "NVDA", "qty": 1.0, "market_ref_px": 223.54}],
            "open_orders": [broker_order],
        },
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T01:07:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert artifact["blockers"] == []
    assert artifact["active_manual_lock_count"] == 0
    assert artifact["scoped_manual_lock_count"] == 1
    assert artifact["records"][0]["classified_as"] == "protected_or_flattening"
    assert artifact["records"][0]["active_manual_lock_count"] == 0
    assert artifact["records"][0]["scoped_manual_lock_count"] == 1
    assert tuple(artifact["records"][0]["scoped_manual_lock_ids"]) == (
        "flatten_action:DU123:nvda_minute_flip_paper_001:NVDA:lot-nvda-1",
    )


@pytest.mark.asyncio
async def test_position_safety_supervisor_manual_required_when_recovery_rejected(tmp_path: Path) -> None:
    lock_store = _FakeManualLockStore()
    recovery_manager = _FakeRecoveryOrderManager(success=False, reason="broker_position_truth_unavailable")
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(quarantine=True, quarantine_symbol="NVDA"),
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=1.0),
        manual_intervention_lock_store=lock_store,
        recovery_order_manager=recovery_manager,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "quarantined"
    assert artifact["records"][0]["active_manual_lock_count"] == 1
    assert lock_store.calls[0]["reason"] == "position_safety_manual_required"


@pytest.mark.asyncio
async def test_position_safety_supervisor_recovers_unprotected_owner_lane_not_symbol_contract(tmp_path: Path) -> None:
    stale_locks = [
        {
            "strategy_id": strategy_id,
            "symbol": "NVDA",
            "reason": "position_safety_manual_required",
            "context": {
                "lock_id": f"position_safety_manual_lock:2026-05-13:{strategy_id}:NVDA",
                "qty": 2.0,
                "reason": "broker_position_without_protection_flatten_quarantine_or_manual_lock",
            },
        }
        for strategy_id in ("nvda_minute_flip_paper_001", "agent_paper_bridge_001", "nvda_minute_flip_paper_002")
    ]
    broker_order = {
        "broker_order_id": "1172",
        "order_ref": "aux:NVDA:982d1323",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1,
        "status": "Submitted",
        "order_type": "MKT",
        "account": "DU123",
    }
    recovery_manager = _FakeRecoveryOrderManager(success=True)
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 1,
                    "broker_order_id": "1172",
                    "order_ref": "aux:NVDA:982d1323",
                    "note": '{"flatten_action":true,"close_only":true,"account_id":"DU123"}',
                }
            ],
            manual_locks=stale_locks,
            ledger_positions={
                ("DU123", "nvda_minute_flip_paper_001", "nvda_minute_flip_paper_001", "NVDA", "lot-1"): 1.0,
                ("DU123", "nvda_minute_flip_paper_002", "nvda_minute_flip_paper_002", "NVDA", "lot-2"): 1.0,
            },
        ),
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=2.0, market_price=227.0, open_orders=[broker_order]),
        manual_intervention_lock_store=_FakeManualLockStore(active_locks=stale_locks),
        recovery_order_manager=recovery_manager,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})(),
            type("Contract", (), {"strategy_id": "agent_paper_bridge_001", "symbols": ("NVDA",), "production_blocking": True})(),
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_002", "symbols": ("NVDA",), "production_blocking": True})(),
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T05:12:00-04:00"))

    record = artifact["records"][0]
    assert tuple(record["contract_strategy_ids"]) == ("nvda_minute_flip_paper_001", "nvda_minute_flip_paper_002")
    assert record["classified_as"] == "recovery_submitted"
    assert tuple(record["manual_lock_ids"]) == (
        "position_safety_manual_lock:2026-05-13:agent_paper_bridge_001:NVDA",
        "position_safety_manual_lock:2026-05-13:nvda_minute_flip_paper_001:NVDA",
        "position_safety_manual_lock:2026-05-13:nvda_minute_flip_paper_002:NVDA",
    )
    assert [call["strategy_id"] for call in supervisor.manual_intervention_lock_store.unlock_calls] == [
        "agent_paper_bridge_001",
        "nvda_minute_flip_paper_001",
        "nvda_minute_flip_paper_002",
    ]
    assert len(recovery_manager.calls) == 1
    result = recovery_manager.calls[0]
    assert result.strategy_id == "nvda_minute_flip_paper_002"
    assert result.plan.qty == 1.0


def test_position_safety_matching_broker_order_accepts_order_ref_after_broker_id_changes() -> None:
    match = PositionSafetySupervisor._matching_broker_open_order(
        {
            "broker_order_id": "1171",
            "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
            "symbol": "NVDA",
            "side": "SELL",
            "qty": 1.0,
        },
        broker_qty=2.0,
        broker_open_orders=[
            {
                "broker_order_id": "1188",
                "order_ref": "sl:DU123|nvda_minute_flip_paper_002|NVDA|",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1.0,
            }
        ],
    )

    assert match is not None
    assert match["broker_order_id"] == "1188"


@pytest.mark.asyncio
async def test_position_safety_supervisor_locks_only_unprotected_owner_lane_when_recovery_rejected(tmp_path: Path) -> None:
    broker_order = {
        "broker_order_id": "1172",
        "order_ref": "aux:NVDA:982d1323",
        "symbol": "NVDA",
        "side": "SELL",
        "qty": 1,
        "status": "Submitted",
        "order_type": "MKT",
        "account": "DU123",
    }
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "nvda_minute_flip_paper_001",
                    "symbol": "NVDA",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 1,
                    "broker_order_id": "1172",
                    "order_ref": "aux:NVDA:982d1323",
                    "note": '{"flatten_action":true,"close_only":true,"account_id":"DU123"}',
                }
            ],
            ledger_positions={
                ("DU123", "nvda_minute_flip_paper_001", "nvda_minute_flip_paper_001", "NVDA", "lot-1"): 1.0,
                ("DU123", "nvda_minute_flip_paper_002", "nvda_minute_flip_paper_002", "NVDA", "lot-2"): 1.0,
            },
        ),
        ib_adapter=_FakePositionAdapter(symbol="NVDA", qty=2.0, market_price=227.0, open_orders=[broker_order]),
        manual_intervention_lock_store=lock_store,
        recovery_order_manager=_FakeRecoveryOrderManager(success=False, reason="submit rejected"),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})(),
            type("Contract", (), {"strategy_id": "agent_paper_bridge_001", "symbols": ("NVDA",), "production_blocking": True})(),
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_002", "symbols": ("NVDA",), "production_blocking": True})(),
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-13T05:12:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert tuple(artifact["records"][0]["manual_lock_ids"]) == (
        "position_safety_manual_lock:2026-05-13:nvda_minute_flip_paper_002:NVDA",
    )
    assert [call["strategy_id"] for call in lock_store.calls] == ["nvda_minute_flip_paper_002"]
    assert lock_store.calls[0]["context"]["qty"] == 1.0


@pytest.mark.asyncio
async def test_position_safety_supervisor_fails_closed_when_broker_open_orders_unavailable(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(open_orders_error=RuntimeError("ib open orders down"))
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(
            open_orders=[
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "state": "SUBMITTED",
                    "side": "SELL",
                    "qty": 10,
                    "broker_order_id": "9001",
                    "note": '{"close_only":true,"flatten_action":true}',
                    "order_ref": "manual_flatten:abc",
                }
            ]
        ),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["records"][0]["classified_as"] == "manual_locked"
    assert artifact["records"][0]["active_order_count"] == 0
    assert artifact["lookup_failed"] is True
    assert artifact["live_broker_snapshot"]["lookup_failed"] is True
    assert lock_store.calls
    assert (tmp_path / "position_safety_latest.json").exists()


@pytest.mark.asyncio
async def test_position_safety_supervisor_fails_closed_when_open_orders_surface_missing(tmp_path: Path) -> None:
    class _NoOpenOrdersAdapter:
        account = "DU123"

        def positions(self):
            return _FakePositionAdapter().positions()

    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=_NoOpenOrdersAdapter(),
        manual_intervention_lock_store=_FakeManualLockStore(),
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["lookup_failed"] is True
    assert artifact["live_broker_snapshot"]["lookup_failed"] is True
    assert artifact["live_broker_snapshot"]["broker_open_order_count"] == 0


@pytest.mark.asyncio
async def test_position_safety_supervisor_rejects_flat_account_live_flatten_order(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        qty=0.0,
        symbol="NVDA",
        open_orders=[
            {
                "broker_order_id": "380",
                "order_ref": "nvda_flip:nvda_minute_flip_paper_001:NVDA:FLA",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1,
                "status": "Submitted",
            }
        ],
    )
    lock_store = _FakeManualLockStore()
    supervisor = PositionSafetySupervisor(
        pg=_FakePositionPg(),
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T23:50:00-04:00"))

    assert artifact["go_no_go"] == "no_go"
    assert artifact["broker_position_count"] == 0
    assert artifact["flat_live_order_count"] == 1
    assert artifact["flat_live_orders"][0]["broker_order_id"] == "380"
    assert artifact["flat_live_orders"][0]["reason"] == "flat_account_live_close_order"


@pytest.mark.asyncio
async def test_position_safety_supervisor_auto_cancels_flat_account_live_safety_order_when_enabled(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=0.0,
        open_orders=[
            {
                "broker_order_id": "936",
                "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1,
                "order_type": "STP",
                "status": "PreSubmitted",
            }
        ],
    )
    lock_store = _FakeManualLockStore()
    pg = _FakePositionPg(
        open_orders=[
            {
                "order_id": "order-936",
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1,
                "order_type": "STP",
                "broker_order_id": "936",
                "note": '{"close_only":true,"protective_stop":true}',
                "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
            }
        ]
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
        flat_live_safety_order_auto_cancel_enabled=True,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T23:50:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert adapter.cancel_calls == [936]
    assert artifact["flat_live_order_count"] == 0
    assert artifact["flat_live_order_cleanup"]["cancelled_count"] == 1
    assert any("UPDATE core.core_order" in sql for sql, _args in pg.executed)


@pytest.mark.asyncio
async def test_position_safety_supervisor_auto_cancels_flat_account_live_safety_order_by_exact_order_ref_when_broker_id_stale(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=0.0,
        open_orders=[
            {
                "broker_order_id": "936",
                "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1,
                "order_type": "STP",
                "status": "PreSubmitted",
            }
        ],
    )
    lock_store = _FakeManualLockStore()
    pg = _FakePositionPg(
        open_orders=[
            {
                "order_id": "order-936",
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1,
                "order_type": "STP",
                "broker_order_id": "stale-db-id",
                "note": '{"close_only":true,"protective_stop":true}',
                "order_ref": "sl:DUO112752|nvda_minute_flip_paper_001|NVDA|",
            }
        ]
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
        flat_live_safety_order_auto_cancel_enabled=True,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T23:50:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert adapter.cancel_calls == [936]
    assert artifact["flat_live_order_count"] == 0
    assert artifact["flat_live_order_cleanup"]["cancelled_count"] == 1
    assert artifact["flat_live_order_cleanup"]["items"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_position_safety_supervisor_auto_cancels_flat_account_live_aux_flatten_order_from_durable_note(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        symbol="NVDA",
        qty=0.0,
        open_orders=[
            {
                "broker_order_id": "1172",
                "order_ref": "aux:NVDA:982d1323",
                "symbol": "NVDA",
                "side": "SELL",
                "qty": 1,
                "order_type": "MKT",
                "status": "PreSubmitted",
            }
        ],
    )
    lock_store = _FakeManualLockStore()
    pg = _FakePositionPg(
        open_orders=[
            {
                "order_id": "order-1172",
                "strategy_id": "nvda_minute_flip_paper_001",
                "symbol": "NVDA",
                "state": "SUBMITTED",
                "side": "SELL",
                "qty": 1,
                "order_type": "MKT",
                "broker_order_id": "1172",
                "note": '{"extras":{"close_only":true,"flatten_action":true}}',
                "order_ref": "aux:NVDA:982d1323",
            }
        ]
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "nvda_minute_flip_paper_001", "symbols": ("NVDA",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
        flat_live_safety_order_auto_cancel_enabled=True,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T23:50:00-04:00"))

    assert artifact["go_no_go"] == "go"
    assert adapter.cancel_calls == [1172]
    assert artifact["flat_live_order_count"] == 0
    assert artifact["flat_live_order_cleanup"]["cancelled_count"] == 1
    update_sql = " ".join(
        sql for sql, _args in pg.executed if "UPDATE core.core_order" in sql
    )
    assert "NULLIF(note, '')" in update_sql
    assert "::text" in update_sql


@pytest.mark.asyncio
async def test_position_safety_supervisor_terminalizes_stale_protection_registry_missing_at_broker(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(open_orders=[])
    lock_store = _FakeManualLockStore()
    pg = _FakePositionPg(
        protection_registry=[
            {
                "protection_key": "DU123|qqq_intraday_001|TQQQ|lot-1|protective_stop|SELL|10|band",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "lot_id": "lot-1",
                "protection_kind": "protective_stop",
                "side": "SELL",
                "desired_qty": 10,
                "broker_order_id": 9001,
                "ib_perm_id": None,
                "order_ref": "protective_stop:abc",
                "state": "SUBMITTED",
            }
        ]
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["protection_registry_sync"]["updated_count"] == 1
    assert pg.executed
    assert pg.executed[0][1][1] == "broker_open_order_missing"


@pytest.mark.asyncio
async def test_position_safety_supervisor_keeps_live_protection_registry_with_broker_open_order(tmp_path: Path) -> None:
    adapter = _FakePositionAdapter(
        open_orders=[
            {
                "broker_order_id": "9001",
                "order_ref": "protective_stop:abc",
                "symbol": "TQQQ",
                "side": "SELL",
                "qty": 10,
                "status": "Submitted",
            }
        ]
    )
    lock_store = _FakeManualLockStore()
    pg = _FakePositionPg(
        protection_registry=[
            {
                "protection_key": "DU123|qqq_intraday_001|TQQQ|lot-1|protective_stop|SELL|10|band",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "lot_id": "lot-1",
                "protection_kind": "protective_stop",
                "side": "SELL",
                "desired_qty": 10,
                "broker_order_id": 9001,
                "ib_perm_id": None,
                "order_ref": "protective_stop:abc",
                "state": "SUBMITTED",
            }
        ]
    )
    supervisor = PositionSafetySupervisor(
        pg=pg,
        ib_adapter=adapter,
        manual_intervention_lock_store=lock_store,
        strategy_data_contracts=[
            type("Contract", (), {"strategy_id": "qqq_intraday_001", "symbols": ("TQQQ",), "production_blocking": True})()
        ],
        artifact_dir=tmp_path,
    )

    artifact = await supervisor.run_once(now=datetime.fromisoformat("2026-05-04T10:00:00-04:00"))

    assert artifact["protection_registry_sync"]["updated_count"] == 0
    assert pg.executed == []
