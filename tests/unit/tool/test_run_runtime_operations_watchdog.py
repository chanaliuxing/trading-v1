from __future__ import annotations

import json
import inspect
import os
import shutil
from datetime import datetime
from pathlib import Path

import tool.ops.run_runtime_operations_watchdog as watchdog
from services.production.ledger_integrity_service import query_position_ledger_integrity


def _write(path: Path, payload: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


def _write_system_health(path: Path, *, ts: str, state: str = "running") -> None:
    _write(
        path,
        {
            "ts": ts,
            "ts_et": ts,
            "domain": "system",
            "component": "system_health_controller",
            "stage": "health_transition",
            "status": state,
            "reason": "runtime_reconciliation_clean",
            "payload": {
                "state": state,
                "event_type": "system_resumed",
                "ts_et": ts,
                "metadata": {
                    "platform_recovery_ready": True,
                    "recovery_readiness": {"ready": True, "blockers": []},
                },
            },
        },
    )


def test_production_state_no_go_when_broker_truth_db_projection_only() -> None:
    state = watchdog._build_production_state(
        {
            "generated_at": "2026-05-11T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "broker_truth_live_snapshot_missing",
                    "severity": "ERROR",
                    "service": "BROKER_TRUTH_SNAPSHOT",
                    "error_type": "broker_truth_live_snapshot_missing",
                }
            ],
            "broker_truth_snapshot": {
                "go_no_go": "no_go",
                "truth_source": "db_projection_only",
                "broker_truth_backed": False,
                "blockers": ["broker_truth_live_snapshot_missing"],
            },
        }
    )

    assert state["go_no_go"] == "no_go"
    assert state["broker_truth_snapshot"]["broker_truth_backed"] is False


def test_production_state_reports_broker_truth_live_snapshot_missing() -> None:
    failures = watchdog._postgres_broker_truth_snapshot_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "require_live_broker_truth": True,
            "broker_truth_backed": False,
            "truth_source": "db_projection_only",
            "blockers": ["broker_truth_live_snapshot_missing"],
        }
    )

    assert {item["issue_key"] for item in failures} == {"broker_truth_live_snapshot_missing"}


def test_postgres_broker_truth_status_uses_fresh_live_snapshot_artifact(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    async def _fake_query(**kwargs):
        captured.update(kwargs)
        return {
            "artifact_type": "broker_truth_snapshot",
            "enabled": True,
            "generated_at": kwargs["generated_at"].isoformat(),
            "go_no_go": "go",
            "lookup_failed": False,
            "require_live_broker_truth": True,
            "broker_truth_backed": True,
            "truth_source": "live_broker_snapshot_plus_db_projection",
            "blockers": [],
        }

    monkeypatch.setattr(watchdog, "_query_postgres_broker_truth_snapshot", _fake_query)
    _write(
        tmp_path / "runtime/readiness/broker_truth_live_snapshot/live_broker_truth_snapshot_latest.json",
        {
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "generated_at": "2026-05-11T10:00:00-04:00",
            "source": "live_broker",
            "broker_truth_backed": True,
            "source_process_pid": 123,
            "account": "DU123",
            "lookup_failed": False,
            "stale": False,
            "go_no_go": "go",
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "positions": [],
            "open_orders": [],
        },
    )

    status = watchdog._postgres_broker_truth_snapshot_status(
        config={"operations": {"runtime_watchdog": {"check_broker_truth_snapshot": True}}},
        repo_root=tmp_path,
        generated_at=datetime(2026, 5, 11, 10, 0, tzinfo=watchdog.ET),
        runtime_version={},
    )

    assert status["broker_truth_backed"] is True
    assert captured["live_broker_snapshot"]["artifact_path"].endswith("live_broker_truth_snapshot_latest.json")


def test_postgres_broker_truth_status_can_produce_manager_snapshot(tmp_path: Path, monkeypatch) -> None:
    async def _fake_manager_snapshot(**kwargs):
        return {
            "artifact_type": "live_broker_truth_snapshot",
            "artifact_present": True,
            "generated_at": kwargs["generated_at"].isoformat(),
            "source": "live_broker",
            "broker_truth_backed": True,
            "source_process_pid": 123,
            "account": "DU123",
            "lookup_failed": False,
            "stale": False,
            "go_no_go": "go",
            "broker_position_count": 0,
            "broker_open_order_count": 0,
            "positions": [],
            "open_orders": [],
        }

    async def _fake_query(**kwargs):
        assert kwargs["live_broker_snapshot"]["source"] == "live_broker"
        return {
            "artifact_type": "broker_truth_snapshot",
            "enabled": True,
            "generated_at": kwargs["generated_at"].isoformat(),
            "go_no_go": "go",
            "lookup_failed": False,
            "require_live_broker_truth": True,
            "broker_truth_backed": True,
            "truth_source": "live_broker_snapshot_plus_db_projection",
            "blockers": [],
        }

    monkeypatch.setattr(watchdog, "build_live_broker_truth_snapshot_from_manager", _fake_manager_snapshot)
    monkeypatch.setattr(watchdog, "_query_postgres_broker_truth_snapshot", _fake_query)

    status = watchdog._postgres_broker_truth_snapshot_status(
        config={
            "operations": {
                "runtime_watchdog": {
                    "check_broker_truth_snapshot": True,
                    "produce_live_broker_truth_snapshot": True,
                    "broker_truth_account": "DU123",
                }
            }
        },
        repo_root=tmp_path,
        generated_at=datetime(2026, 5, 11, 10, 0, tzinfo=watchdog.ET),
        runtime_version={},
    )

    assert status["broker_truth_backed"] is True


def test_production_state_explains_position_safety_dependency_failure() -> None:
    failures = watchdog._position_safety_check(
        {
            "artifact_present": True,
            "artifact_path": "runtime/readiness/position_safety/position_safety_latest.json",
            "stale": False,
            "go_no_go": "no_go",
            "lookup_failed": True,
            "unsafe_position_count": 0,
            "active_quarantine_count": 0,
            "records": [],
            "blockers": ["broker_truth_missing"],
            "dependency_state": {
                "broker_truth_required": True,
                "broker_truth_available": False,
                "broker_truth_backed": False,
            },
        }
    )

    keys = {item["issue_key"] for item in failures}
    assert "position_safety_broker_truth_missing" in keys
    assert "position_safety_lookup_failed" in keys


def test_watchdog_runs_broker_truth_before_position_safety() -> None:
    source = inspect.getsource(watchdog.build_runtime_operations_watchdog_report)

    assert source.index("broker_truth_snapshot = _postgres_broker_truth_snapshot_status") < source.index(
        "position_safety = _position_safety_status"
    )


def test_disk_space_check_blocks_low_free_space(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        watchdog.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=10 * 1024**3, used=9 * 1024**3 + 1, free=1024**3 - 1),
    )

    status = watchdog._disk_space_status(
        root=tmp_path,
        config={"operations": {"runtime_watchdog": {"min_free_disk_bytes": 1024**3}}},
    )
    failures = watchdog._disk_space_check(status)

    assert status["go_no_go"] == "no_go"
    assert status["blockers"] == ["disk_free_space_below_minimum"]
    assert failures[0]["issue_key"] == "disk_free_space_below_minimum"


def test_disk_space_check_passes_when_free_space_above_minimum(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        watchdog.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=10 * 1024**3, used=8 * 1024**3, free=2 * 1024**3),
    )

    status = watchdog._disk_space_status(
        root=tmp_path,
        config={"operations": {"runtime_watchdog": {"min_free_disk_bytes": 1024**3}}},
    )

    assert status["go_no_go"] == "go"
    assert watchdog._disk_space_check(status) == []


class _FakeManager:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_now(self, event):
        self.events.append(
            {
                "event_type": event.event_type,
                "status": event.status,
                "symbol": event.symbol,
                "reason": event.reason,
                "metadata": dict(event.metadata or {}),
            }
        )
        return True

    async def shutdown(self) -> None:
        return None


def test_watchdog_flags_missing_opening_artifact_and_sends_alert(tmp_path: Path, monkeypatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: manager)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    _write(
        tmp_path / "config.yml",
        {
            "notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}},
            "operations": {"runtime_watchdog": {"check_disk_space": False}},
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:34:30-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:34:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T09:34:30-04:00", "stage": "gate_blocked"},
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "opening_artifact_missing:preopen" in failure_keys
    assert "opening_artifact_missing:open" in failure_keys
    assert "opening_artifact_missing:acceptance" in failure_keys
    assert manager.events


def test_watchdog_flags_stale_qqq_strategy_and_emits_recovery_once(tmp_path: Path, monkeypatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: manager)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        watchdog,
        "_process_start_ts",
        lambda pid: datetime(2026, 4, 24, 9, 0, tzinfo=watchdog.ET).timestamp(),
    )
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(
        watchdog,
        "_postgres_broker_truth_snapshot_status",
        lambda **kwargs: {
            "artifact_type": "broker_truth_snapshot",
            "enabled": True,
            "go_no_go": "go",
            "lookup_failed": False,
            "require_live_broker_truth": True,
            "broker_truth_backed": True,
            "broker_truth_projection_clean": True,
            "order_truth_active_quarantine_count": 0,
            "projection_divergence_count": 0,
            "blockers": [],
        },
    )
    _write(
        tmp_path / "config.yml",
        {
            "notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}},
            "operations": {"runtime_watchdog": {"check_disk_space": False}},
        },
    )

    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:20:00-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:20:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )

    first = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 35, tzinfo=watchdog.ET),
        state_path=tmp_path / "runtime/state/watchdog_alert_state.json",
    )

    assert any(item["issue_key"] == "qqq_strategy_stale:observability" for item in first["failures"])
    assert any(event["status"] == "ERROR" for event in manager.events)
    first_event_count = len(manager.events)

    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:44:30-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T09:44:30-04:00", "stage": "gate_blocked"},
    )
    _write(tmp_path / "runtime/state/opening_day/20260424_preopen_supervisor.json", {"final": {"phase": "preopen"}})
    _write(tmp_path / "runtime/state/opening_day/20260424_open_supervisor.json", {"final": {"phase": "open"}})
    _write(tmp_path / "runtime/state/opening_day/20260424_acceptance_supervisor.json", {"final": {"phase": "acceptance"}})
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-09.jsonl",
        ts="2026-04-24T09:44:30-04:00",
    )
    _write(
        tmp_path / "runtime/readiness/position_safety/position_safety_latest.json",
        {
            "artifact_type": "position_safety",
            "generated_at": "2026-04-24T09:44:30-04:00",
            "go_no_go": "go",
            "lookup_failed": False,
            "broker_position_count": 0,
            "unsafe_position_count": 0,
            "records": [],
            "live_broker_snapshot": {
                "artifact_type": "live_broker_truth_snapshot",
                "artifact_present": True,
                "generated_at": "2026-04-24T09:44:30-04:00",
                "source_process_pid": 123,
                "account": "DU123",
                "lookup_failed": False,
                "stale": False,
                "go_no_go": "go",
                "broker_position_count": 0,
                "broker_open_order_count": 0,
                "positions": [],
                "open_orders": [],
            },
        },
    )
    _write(
        tmp_path / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json",
        {
            "artifact_type": "recovery_incidents",
            "generated_at": "2026-04-24T09:44:30-04:00",
            "active_count": 0,
            "terminal_count": 0,
            "incidents": [],
        },
    )

    second = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
        state_path=tmp_path / "runtime/state/watchdog_alert_state.json",
    )

    assert second["failure_count"] == 0
    assert len(manager.events) > first_event_count
    assert any(event["status"] == "INFO" for event in manager.events[first_event_count:])
    production_state_path = tmp_path / "runtime/readiness/production_state/production_state_latest.json"
    production_state = json.loads(production_state_path.read_text(encoding="utf-8"))
    assert production_state["artifact_type"] == "production_state"
    assert production_state["go_no_go"] == "go"
    assert production_state["failure_count"] == 0
    assert "recovery_incidents" in production_state
    assert "fill_attribution" in production_state
    assert "ledger_integrity" in production_state
    assert production_state["runtime_version"]["process_pid"]
    assert "git_dirty" in production_state["runtime_version"]
    assert production_state["runtime_version"]["config_hash"]
    assert isinstance(production_state["runtime_version"]["config_files"], list)
    legacy_state_path = tmp_path / "runtime/readiness/production_state_latest.json"
    legacy_state = json.loads(legacy_state_path.read_text(encoding="utf-8"))
    assert legacy_state["deprecated_alias"] is True
    assert legacy_state["canonical_path"] == str(production_state_path)
    assert legacy_state["generated_at"] == production_state["generated_at"]
    assert legacy_state["go_no_go"] == production_state["go_no_go"]

    watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
        output_path=production_state_path,
        state_path=tmp_path / "runtime/state/watchdog_alert_state_second.json",
    )
    overwritten_state = json.loads(production_state_path.read_text(encoding="utf-8"))
    assert overwritten_state["artifact_type"] == "production_state"
    assert "production_state" not in overwritten_state
    assert overwritten_state["go_no_go"] == "go"


def test_position_safety_check_rejects_go_with_quarantined_record(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/position_safety/position_safety_latest.json",
        {
            "artifact_type": "position_safety",
            "generated_at": "2026-04-24T09:44:30-04:00",
            "go_no_go": "go",
            "lookup_failed": False,
            "broker_position_count": 1,
            "unsafe_position_count": 0,
            "active_quarantine_count": 0,
            "records": [
                {
                    "symbol": "NVDA",
                    "classified_as": "quarantined",
                    "active_quarantine": True,
                }
            ],
        },
    )

    status = watchdog._position_safety_status(
        root=tmp_path,
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
    )
    failures = watchdog._position_safety_check(status)

    assert "position_safety_contradictory_go" in {item["issue_key"] for item in failures}


def test_contract_selection_check_blocks_fresh_rejection(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/contract_selection/contract_selection_latest.json",
        {
            "artifact_type": "contract_selection_verdict",
            "generated_at": "2026-04-24T09:45:00-04:00",
            "strategy_id": "qqq_intraday_option_v1_001",
            "symbol": "QQQ",
            "status": "rejected",
            "reason": "no_contract_selection",
        },
    )

    status = watchdog._contract_selection_status(
        root=tmp_path,
        now_et=datetime(2026, 4, 24, 9, 45, 30, tzinfo=watchdog.ET),
    )
    failures = watchdog._contract_selection_check(status)

    assert status["ok"] is False
    assert failures[0]["issue_key"].startswith("contract_selection:")


def test_contract_selection_check_blocks_missing_verdict_for_recent_option_handoff(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/state/qqq_stock_option_handoff.jsonl",
        {
            "trigger_key": "trigger-1",
            "trigger_id": "trigger-1",
            "source_strategy_id": "qqq_intraday_001",
            "consumer_strategy_id": "qqq_intraday_option_v1_001",
            "underlying": "QQQ",
            "signal_family": "qqq_intraday_entry",
            "status": "published",
            "reason": "stock_signal_published",
            "ts": "2026-04-24T09:45:00-04:00",
            "terminal": False,
        },
    )

    status = watchdog._contract_selection_status(
        root=tmp_path,
        now_et=datetime(2026, 4, 24, 9, 45, 30, tzinfo=watchdog.ET),
        required_option_strategy_ids=["qqq_intraday_option_v1_001"],
    )
    failures = watchdog._contract_selection_check(status)

    assert status["ok"] is False
    assert status["reason"] == "missing_contract_selection_verdict"
    assert failures[0]["issue_key"].startswith("contract_selection:")


def test_option_trigger_ledger_check_blocks_active_and_rejected() -> None:
    failures = watchdog._option_trigger_ledger_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "active_trigger_count": 1,
            "rejected_trigger_count": 1,
            "required_strategy_ids": ["qqq_intraday_option_v1_001"],
        }
    )
    keys = {item["issue_key"] for item in failures}

    assert "option_trigger_active_without_terminal_verdict" in keys
    assert "option_trigger_rejected_contract_selection" in keys
    assert all(item["required_strategy_ids"] == ["qqq_intraday_option_v1_001"] for item in failures)


def test_new_artifact_checks_block_stale_or_pre_process_runtime_contract() -> None:
    fill_failures = watchdog._postgres_fill_attribution_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "unknown_fill_count": 0,
            "ambiguous_fill_count": 0,
            "stale": True,
            "age_sec": 91.0,
            "max_age_sec": 90.0,
            "artifact_before_process_start": True,
            "generated_at": "2026-04-24T09:29:00-04:00",
            "source_process_start_time": "2026-04-24T09:30:00-04:00",
        }
    )
    broker_failures = watchdog._postgres_broker_truth_snapshot_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "order_truth_active_quarantine_count": 0,
            "projection_divergence_count": 0,
            "stale": True,
            "age_sec": 91.0,
            "max_age_sec": 90.0,
        }
    )
    option_failures = watchdog._option_trigger_ledger_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "active_trigger_count": 0,
            "rejected_trigger_count": 0,
            "artifact_before_process_start": True,
            "generated_at": "2026-04-24T09:29:00-04:00",
            "source_process_start_time": "2026-04-24T09:30:00-04:00",
        }
    )

    assert {
        "fill_attribution_artifact_stale",
        "fill_attribution_artifact_before_process_start",
    }.issubset({item["issue_key"] for item in fill_failures})
    assert "broker_truth_snapshot_artifact_stale" in {item["issue_key"] for item in broker_failures}
    assert "option_trigger_ledger_artifact_before_process_start" in {
        item["issue_key"] for item in option_failures
    }


def test_strategy_manifest_gate_check_surfaces_blockers() -> None:
    failures = watchdog._strategy_manifest_gate_check(
        {
            "ok": False,
            "blockers": ["paper_live_001:force_flatten_deadline_missing_or_invalid"],
        }
    )

    assert failures[0]["issue_key"].startswith("strategy_manifest_gate:")
    assert failures[0]["service"] == "STRATEGY_MANIFEST_GATE"


def test_position_ledger_integrity_failures_surface_unknown_account_and_divergence() -> None:
    failures = watchdog._postgres_ledger_integrity_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "issue_count": 2,
            "account_divergence_count": 1,
            "zero_qty_notional_residual_count": 3,
            "zero_qty_notional_residual_threshold": 1.0,
            "issues": [],
            "account_divergences": [],
            "zero_qty_notional_residuals": [],
        }
    )

    issue_keys = {item["issue_key"] for item in failures}
    assert "position_ledger_unknown_account_or_null_lot" in issue_keys
    assert "position_ledger_account_divergence" in issue_keys
    assert "position_ledger_zero_qty_notional_residual" in issue_keys


def test_fill_attribution_failures_surface_unknown_fills() -> None:
    failures = watchdog._postgres_fill_attribution_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "unknown_fill_count": 2,
            "ambiguous_fill_count": 1,
        }
    )

    assert failures[0]["issue_key"] == "unknown_or_ambiguous_broker_fills_active"
    assert failures[0]["unknown_fill_count"] == 2
    assert failures[0]["ambiguous_fill_count"] == 1


def test_recovery_incidents_status_blocks_active_incident(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json",
        {
            "artifact_type": "recovery_incidents",
            "generated_at": "2026-05-05T10:00:00-04:00",
            "active_count": 1,
            "terminal_count": 0,
            "incidents": [{"incident_id": "recovery_incident:test:TQQQ"}],
        },
    )

    status = watchdog._recovery_incidents_status(
        root=tmp_path,
        now_et=datetime(2026, 5, 5, 10, 0, 30, tzinfo=watchdog.ET),
    )
    failures = watchdog._recovery_incidents_check(status)

    assert status["go_no_go"] == "no_go"
    assert failures[0]["issue_key"] == "active_recovery_incidents_present"


def test_recovery_incidents_check_reports_awaiting_market_reopen(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json",
        {
            "artifact_type": "recovery_incidents",
            "generated_at": "2026-05-05T10:00:00-04:00",
            "active_count": 1,
            "active_intent_count": 1,
            "awaiting_market_reopen_count": 1,
            "terminal_count": 0,
            "incidents": [
                {
                    "incident_id": "recovery_incident:test:OPT:NVDA:20260506:207.5:P",
                    "state": "awaiting_market_reopen",
                    "active_recovery_intent": {
                        "next_action": "wait_until_market_reopen",
                        "next_retry_after": "2026-05-06T09:30:00-04:00",
                    },
                }
            ],
        },
    )

    status = watchdog._recovery_incidents_status(
        root=tmp_path,
        now_et=datetime(2026, 5, 5, 10, 0, 30, tzinfo=watchdog.ET),
    )
    failures = watchdog._recovery_incidents_check(status)

    assert status["go_no_go"] == "no_go"
    assert failures[0]["issue_key"] == "recovery_incidents_awaiting_market_reopen"
    assert failures[0]["awaiting_market_reopen_not_before"] == "2026-05-06T09:30:00-04:00"


def test_recovery_incidents_status_accepts_fresh_empty_artifact(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json",
        {
            "artifact_type": "recovery_incidents",
            "generated_at": "2026-05-05T10:00:00-04:00",
            "active_count": 0,
            "terminal_count": 0,
            "incidents": [],
        },
    )

    status = watchdog._recovery_incidents_status(
        root=tmp_path,
        now_et=datetime(2026, 5, 5, 10, 0, 30, tzinfo=watchdog.ET),
    )

    assert status["go_no_go"] == "go"
    assert watchdog._recovery_incidents_check(status) == []


def test_recovery_incidents_check_blocks_terminal_active_quarantine(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json",
        {
            "artifact_type": "recovery_incidents",
            "generated_at": "2026-05-05T10:00:00-04:00",
            "active_count": 0,
            "terminal_count": 1,
            "incidents": [
                {
                    "incident_id": "recovery_incident:test:TQQQ",
                    "state": "verified_flat",
                    "terminal": True,
                    "active_quarantine": True,
                    "verified_flat_at": "2026-05-05T10:00:00-04:00",
                }
            ],
        },
    )

    status = watchdog._recovery_incidents_status(
        root=tmp_path,
        now_et=datetime(2026, 5, 5, 10, 0, 30, tzinfo=watchdog.ET),
    )
    failures = watchdog._recovery_incidents_check(status)

    assert "recovery_incident_terminal_active_quarantine" in {item["issue_key"] for item in failures}


def test_recovery_incidents_check_blocks_terminal_active_intent_and_flattening(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json",
        {
            "artifact_type": "recovery_incidents",
            "generated_at": "2026-05-05T10:00:00-04:00",
            "active_count": 0,
            "terminal_count": 1,
            "incidents": [
                {
                    "incident_id": "recovery_incident:test:TQQQ",
                    "state": "verified_flat",
                    "terminal": True,
                    "active_quarantine": False,
                    "protected_or_flattening": True,
                    "active_recovery_intent": {"intent_id": "recovery:1"},
                    "verified_flat_at": "2026-05-05T10:00:00-04:00",
                }
            ],
        },
    )

    status = watchdog._recovery_incidents_status(
        root=tmp_path,
        now_et=datetime(2026, 5, 5, 10, 0, 30, tzinfo=watchdog.ET),
    )
    failures = watchdog._recovery_incidents_check(status)
    keys = {item["issue_key"] for item in failures}

    assert "recovery_incident_terminal_still_protected_or_flattening" in keys
    assert "recovery_incident_terminal_active_intent" in keys


def test_recovery_incidents_check_blocks_verified_flat_missing_evidence(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/recovery_incidents/recovery_incidents_latest.json",
        {
            "artifact_type": "recovery_incidents",
            "generated_at": "2026-05-05T10:00:00-04:00",
            "active_count": 0,
            "terminal_count": 1,
            "incidents": [
                {
                    "incident_id": "recovery_incident:test:TQQQ",
                    "state": "verified_flat",
                    "terminal": True,
                    "active_quarantine": False,
                }
            ],
        },
    )

    status = watchdog._recovery_incidents_status(
        root=tmp_path,
        now_et=datetime(2026, 5, 5, 10, 0, 30, tzinfo=watchdog.ET),
    )
    failures = watchdog._recovery_incidents_check(status)

    assert "recovery_incident_verified_flat_missing_evidence" in {item["issue_key"] for item in failures}


def test_position_ledger_integrity_flags_offsetting_null_lot_residual_sql() -> None:
    sql = query_position_ledger_integrity.__code__.co_consts
    joined = "\n".join(str(item) for item in sql if isinstance(item, str))

    assert "ledger_net.account_id = ''" in joined
    assert "ledger_net.lot_id = ''" in joined
    assert "abs(symbol_net.net_qty) > 1e-9" not in joined


def test_broker_truth_snapshot_check_blocks_quarantine_and_projection_divergence() -> None:
    failures = watchdog._postgres_broker_truth_snapshot_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "require_live_broker_truth": True,
            "broker_truth_backed": False,
            "order_truth_active_quarantine_count": 1,
            "projection_divergence_count": 1,
        }
    )
    keys = {item["issue_key"] for item in failures}

    assert "broker_truth_live_snapshot_missing" in keys
    assert "broker_truth_active_order_quarantine" in keys
    assert "broker_truth_position_projection_divergence" in keys


def test_position_ledger_integrity_status_writes_artifact_when_disabled(tmp_path: Path) -> None:
    status = watchdog._postgres_ledger_integrity_status(
        config={"operations": {"runtime_watchdog": {"check_position_ledger_integrity": False}}},
        repo_root=tmp_path,
        generated_at=datetime(2026, 5, 5, 10, 0, tzinfo=watchdog.ET),
    )
    artifact = json.loads(
        (tmp_path / "runtime/readiness/ledger_integrity/ledger_integrity_latest.json").read_text(encoding="utf-8")
    )

    assert status["artifact_type"] == "ledger_integrity"
    assert status["go_no_go"] == "go"
    assert artifact["enabled"] is False
    assert artifact["position_ledger_clean"] is True


def test_runtime_version_check_blocks_stale_loaded_code() -> None:
    failures = watchdog._runtime_version_check(
        {
            "loaded_code_current": False,
            "process_pid": 123,
            "process_start_time": "2026-04-24T09:30:00-04:00",
            "stale_modules": [{"path": "services/production/production_state_emitter.py"}],
        }
    )

    assert failures[0]["issue_key"] == "runtime_version_loaded_code_stale"
    assert failures[0]["stale_modules"][0]["path"] == "services/production/production_state_emitter.py"


def test_manual_required_visible_as_manual_recovery_required_not_generic_startup_retry() -> None:
    failures = watchdog._system_health_check(
        {
            "present": True,
            "unhealthy": True,
            "state": "degraded",
            "reason": "manual_recovery_required",
            "raw": {
                "payload": {
                    "metadata": {
                        "ib_gateway_circuit": {
                            "opened_at": "2026-05-12T10:00:00+00:00",
                            "source": "market:pre_connect_api_probe",
                            "last_proof_status": "PROOF_FAILED",
                        }
                    }
                }
            },
        }
    )

    assert failures[0]["issue_key"] == "manual_gateway_api_recovery_required"
    assert failures[0]["error_type"] == "manual_recovery_required"
    assert failures[0]["required_action"] == "operator_ack_then_single_lease_gateway_recovery_proof"


def test_runtime_freshness_stale_keeps_production_no_go_for_ib_recovery_modules() -> None:
    state = watchdog._build_production_state(
        {
            "generated_at": "2026-05-12T10:00:00-04:00",
            "failures": [
                {
                    "issue_key": "runtime_version_loaded_code_stale",
                    "severity": "ERROR",
                    "service": "RUNTIME_VERSION",
                    "error_type": "runtime_version_loaded_code_stale",
                    "detail": "run_system_process_started_before_modified_critical_modules",
                    "stale_modules": [{"path": "services/ibkr/ib_gateway_recovery_coordinator.py"}],
                }
            ],
            "strategy_tradability": {},
        }
    )

    assert state["go_no_go"] == "no_go"
    assert state["safety_go_no_go"] == "no_go"
    assert state["runtime_failure_count"] == 1


def test_runtime_fresh_when_process_started_after_critical_modules() -> None:
    result = watchdog.evaluate_runtime_code_freshness(
        process_start_time="2026-05-12T10:00:00-04:00",
        critical_modules=[
            {
                "path": "services/exec/safety_kernel.py",
                "modified_at": "2026-05-12T09:59:00-04:00",
            }
        ],
        git_sha="abc",
        build_id="build-1",
        source_tree_hash="tree-1",
        now=datetime(2026, 5, 12, 10, 1, tzinfo=watchdog.ET),
    )

    assert result["status"] == "CURRENT"
    assert result["loaded_code_current"] is True
    assert result["runtime_version_loaded_code_stale"] is False
    assert result["restart_required"] is False
    assert result["blockers"] == []


def test_runtime_stale_when_critical_module_modified_after_process_start() -> None:
    result = watchdog.evaluate_runtime_code_freshness(
        process_start_time="2026-05-12T10:00:00-04:00",
        critical_modules=[
            {
                "path": "services/exec/safety_kernel.py",
                "modified_at": "2026-05-12T10:05:00-04:00",
            }
        ],
        now=datetime(2026, 5, 12, 10, 6, tzinfo=watchdog.ET),
    )

    assert result["status"] == "STALE_CRITICAL_MODULE"
    assert result["loaded_code_current"] is False
    assert result["restart_required"] is True
    assert result["blockers"] == ["runtime_version_loaded_code_stale"]
    assert result["stale_modules"][0]["path"] == "services/exec/safety_kernel.py"


def test_runtime_unknown_when_manifest_missing_fails_closed() -> None:
    result = watchdog.evaluate_runtime_code_freshness(
        process_start_time="2026-05-12T10:00:00-04:00",
        critical_modules=[],
        now=datetime(2026, 5, 12, 10, 1, tzinfo=watchdog.ET),
    )

    assert result["status"] == "MISSING_MANIFEST"
    assert result["loaded_code_current"] is False
    assert result["restart_required"] is True
    assert result["blockers"] == ["runtime_version_missing_manifest"]


def test_runtime_build_mismatch_blocks_when_hard_required() -> None:
    result = watchdog.evaluate_runtime_code_freshness(
        process_start_time="2026-05-12T10:00:00-04:00",
        critical_modules=[
            {
                "path": "services/exec/safety_kernel.py",
                "modified_at": "2026-05-12T09:59:00-04:00",
            }
        ],
        git_sha="runtime-sha",
        expected_git_sha="expected-sha",
        build_id="runtime-build",
        expected_build_id="expected-build",
        now=datetime(2026, 5, 12, 10, 1, tzinfo=watchdog.ET),
    )

    assert result["status"] == "BUILD_MISMATCH"
    assert result["loaded_code_current"] is False
    assert result["restart_required"] is True
    assert "runtime_version_build_mismatch" in result["blockers"]


def test_runtime_version_check_blocks_missing_manifest_and_build_mismatch() -> None:
    missing = watchdog._runtime_version_check(
        {
            "loaded_code_current": False,
            "freshness": {
                "status": "MISSING_MANIFEST",
                "loaded_code_current": False,
                "restart_required": True,
                "blockers": ["runtime_version_missing_manifest"],
            },
        }
    )
    mismatch = watchdog._runtime_version_check(
        {
            "loaded_code_current": False,
            "freshness": {
                "status": "BUILD_MISMATCH",
                "loaded_code_current": False,
                "restart_required": True,
                "blockers": ["runtime_version_build_mismatch"],
            },
        }
    )

    assert missing[0]["issue_key"] == "runtime_version_missing_manifest"
    assert mismatch[0]["issue_key"] == "runtime_version_build_mismatch"


def test_restart_proof_artifact_contains_pid_start_time_and_stale_modules(tmp_path: Path, monkeypatch) -> None:
    module_path = tmp_path / "services/exec/safety_kernel.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("# changed\n", encoding="utf-8")
    monkeypatch.setattr(watchdog, "CRITICAL_RUNTIME_MODULES", ["services/exec/safety_kernel.py"])
    monkeypatch.setattr(watchdog, "_run_system_status", lambda root: {"process_pids": [123], "lock_pid": 123, "lock_pid_alive": True})
    monkeypatch.setattr(watchdog, "_process_start_ts", lambda pid: datetime(2026, 5, 12, 9, 30, tzinfo=watchdog.ET).timestamp())
    changed_at = datetime(2026, 5, 12, 9, 35, tzinfo=watchdog.ET).timestamp()
    os.utime(module_path, (changed_at, changed_at))

    status = watchdog._runtime_version_status(tmp_path)

    assert status["process_pid"] == 123
    assert status["process_start_time"] == "2026-05-12T09:30:00-04:00"
    assert status["runtime_id"].startswith("123:")
    assert status["restart_id"].startswith("123:")
    assert status["freshness"]["restart_required"] is True
    assert status["freshness"]["critical_modules_stale_count"] == 1
    assert status["stale_modules"][0]["path"] == "services/exec/safety_kernel.py"


def test_reconcile_strategy_tradability_gate_ignores_superseded_gate() -> None:
    status = {
        "ok": False,
        "gates": [
            {
                "dataset": "strategy_tradability",
                "strategy_id": "qqq_intraday_001",
                "symbol": "QQQ",
                "resolution": "entry",
                "stage": "stable_entry_guard",
                "reason": "first_entry_seed_not_ready",
                "severity": "entry_blocking",
                "rth": True,
                "last_seen": "2026-05-06T10:22:00.255858-04:00",
            }
        ],
        "ignored_gates": [],
    }

    reconciled = watchdog._reconcile_strategy_tradability_data_gates(
        status,
        strategy_heartbeats={
            "qqq_intraday_001": {
                "strategy_id": "qqq_intraday_001",
                "tradable": True,
                "stale": False,
                "generated_at": "2026-05-06T10:22:00-04:00",
            }
        },
    )

    assert reconciled["ok"] is True
    assert reconciled["active_gate_count"] == 0
    assert reconciled["ignored_gate_count"] == 1
    assert reconciled["ignored_gates"][0]["ignored_reason"] == "superseded_by_fresh_tradable_heartbeat"


def test_reconcile_strategy_tradability_gate_keeps_newer_gate() -> None:
    status = {
        "gates": [
            {
                "dataset": "strategy_tradability",
                "strategy_id": "qqq_intraday_001",
                "symbol": "QQQ",
                "resolution": "entry",
                "stage": "stable_entry_guard",
                "reason": "first_entry_seed_not_ready",
                "severity": "entry_blocking",
                "rth": True,
                "last_seen": "2026-05-06T10:23:00-04:00",
            }
        ],
        "ignored_gates": [],
    }

    reconciled = watchdog._reconcile_strategy_tradability_data_gates(
        status,
        strategy_heartbeats={
            "qqq_intraday_001": {
                "strategy_id": "qqq_intraday_001",
                "tradable": True,
                "stale": False,
                "generated_at": "2026-05-06T10:22:00-04:00",
            }
        },
    )

    assert reconciled["ok"] is False
    assert reconciled["active_gate_count"] == 1
    assert reconciled["entry_blocking_count"] == 1


def test_reconcile_strategy_tradability_gate_ignores_entry_gate_outside_strategy_session() -> None:
    status = {
        "gates": [
            {
                "dataset": "strategy_tradability",
                "strategy_id": "qqq_intraday_001",
                "symbol": "QQQ",
                "resolution": "entry",
                "stage": "stable_entry_guard",
                "reason": "data_integrity_fail_closed",
                "severity": "entry_blocking",
                "rth": False,
                "last_seen": "2026-05-06T22:19:55-04:00",
            }
        ],
        "ignored_gates": [],
    }

    reconciled = watchdog._reconcile_strategy_tradability_data_gates(
        status,
        strategy_heartbeats={
            "qqq_intraday_001": {
                "strategy_id": "qqq_intraday_001",
                "tradable": True,
                "stale": False,
                "generated_at": "2026-05-06T22:19:18-04:00",
                "details": {
                    "strategy_session_open": False,
                    "loop_last_stage": "system_paused",
                },
            }
        },
    )

    assert reconciled["ok"] is True
    assert reconciled["active_gate_count"] == 0
    assert reconciled["ignored_gates"][0]["ignored_reason"] == "entry_gate_outside_strategy_session"


def test_strategy_tradability_check_ignores_pause_derived_only_blocker() -> None:
    failures = watchdog._strategy_tradability_check(
        {
            "go_no_go": "no_go",
            "blockers": [
                "qqq_intraday_001:system_paused",
                "stocks_kernel_smh_001:system_paused",
            ],
        }
    )

    assert failures == []


def test_strategy_tradability_check_keeps_real_blocker_with_system_paused() -> None:
    failures = watchdog._strategy_tradability_check(
        {
            "go_no_go": "no_go",
            "blockers": [
                "qqq_intraday_001:system_paused",
                "qqq_intraday_001:stream_invalid",
            ],
        }
    )

    assert [item["detail"] for item in failures] == ["qqq_intraday_001:stream_invalid"]


def test_strategy_tradability_check_ignores_idle_and_stale_as_entry_failures() -> None:
    failures = watchdog._strategy_tradability_check(
        {
            "go_no_go": "no_go",
            "blockers": [
                "nvda_option_churn_smoke_001:idle_not_in_entry_window",
                "qqq_intraday_001:fresh_tradability_heartbeat_stale",
            ],
        }
    )

    assert failures == []


def test_qqq_offsession_no_longer_reports_entry_tradable() -> None:
    summary = watchdog._qqq_liveness_summary(
        obs={
            "ready": False,
            "missing_required": ["QQQ:last_stale"],
            "required_values": {"QQQ": {"age_sec": 999.0}},
        },
        obs_age=10.0,
        diag_last={"strategy_session_open": False, "stream_invalid": True},
        diag_age=10.0,
        qqq_audit={"row_count": 1},
        stale_sec=180.0,
    )

    assert summary["alive"] is True
    assert summary["session_open"] is False
    assert summary["tradable"] is False
    assert summary["tradable_entry_ready"] is False
    assert summary["idle_reason"] == "offsession"


def test_reconcile_strategy_tradability_gate_ignores_stale_gate_after_blocker_change() -> None:
    status = {
        "ok": False,
        "gates": [
            {
                "dataset": "strategy_tradability",
                "strategy_id": "qqq_intraday_001",
                "symbol": "QQQ",
                "resolution": "entry",
                "stage": "stable_entry_guard",
                "reason": "data_integrity_fail_closed",
                "severity": "entry_blocking",
                "rth": True,
                "last_seen": "2026-05-06T14:58:21-04:00",
            }
        ],
        "ignored_gates": [],
    }

    reconciled = watchdog._reconcile_strategy_tradability_data_gates(
        status,
        strategy_heartbeats={
            "qqq_intraday_001": {
                "strategy_id": "qqq_intraday_001",
                "tradable": False,
                "stale": False,
                "generated_at": "2026-05-06T15:00:00-04:00",
                "blockers": ["stream_invalid"],
            }
        },
    )

    assert reconciled["ok"] is True
    assert reconciled["active_gate_count"] == 0
    assert reconciled["entry_blocking_count"] == 0
    assert reconciled["ignored_gates"][0]["ignored_reason"] == "superseded_by_fresh_heartbeat_blocker_change"


def test_reconcile_strategy_tradability_gate_keeps_same_active_blocker() -> None:
    status = {
        "gates": [
            {
                "dataset": "strategy_tradability",
                "strategy_id": "qqq_intraday_001",
                "symbol": "QQQ",
                "resolution": "entry",
                "stage": "stable_entry_guard",
                "reason": "data_integrity_fail_closed",
                "severity": "entry_blocking",
                "rth": True,
                "last_seen": "2026-05-06T14:58:21-04:00",
            }
        ],
        "ignored_gates": [],
    }

    reconciled = watchdog._reconcile_strategy_tradability_data_gates(
        status,
        strategy_heartbeats={
            "qqq_intraday_001": {
                "strategy_id": "qqq_intraday_001",
                "tradable": False,
                "stale": False,
                "generated_at": "2026-05-06T15:00:00-04:00",
                "blockers": ["stream_invalid", "stable_entry_guard:data_integrity_fail_closed"],
            }
        },
    )

    assert reconciled["ok"] is False
    assert reconciled["active_gate_count"] == 1
    assert reconciled["entry_blocking_count"] == 1


def test_system_health_check_ignores_production_state_self_degraded() -> None:
    failures = watchdog._system_health_check(
        {
            "present": True,
            "unhealthy": True,
            "state": "degraded",
            "reason": "production_state_no_go",
            "stale": False,
            "age_sec": 10.0,
            "max_age_sec": 90.0,
        }
    )

    assert failures == []


def test_run_system_status_ignores_wrapper_process_matches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [10, 11, 12])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "_is_actual_run_system_process", lambda pid: pid == 12)
    _write(tmp_path / "runtime/locks/run_system.lock", "12\n")

    status = watchdog._run_system_status(tmp_path)

    assert status["alive"] is True
    assert status["process_pids"] == [12]
    assert status["ignored_process_pids"] == [10, 11]
    assert status["lock_matches_process"] is True


def test_ib_restart_drill_check_blocks_artifact_before_process_start() -> None:
    failures = watchdog._ib_restart_drill_check(
        {
            "required": True,
            "artifact_present": True,
            "artifact_day": "2026-04-24",
            "expected_day": "2026-04-24",
            "go_no_go": "go",
            "blockers": [],
            "artifact_before_process_start": True,
            "generated_at": "2026-04-24T10:00:00-04:00",
            "process_start_time": "2026-04-24T10:30:00-04:00",
            "artifact_path": "/tmp/paper_ib_restart_latest.json",
        }
    )

    assert "ib_restart_drill_artifact_before_process_start" in {item["issue_key"] for item in failures}


def test_ib_restart_drill_check_blocks_existing_no_go_artifact_even_when_not_required() -> None:
    failures = watchdog._ib_restart_drill_check(
        {
            "required": False,
            "artifact_present": True,
            "artifact_day": "2026-04-24",
            "expected_day": "2026-04-24",
            "go_no_go": "no_go",
            "blockers": ["post_resume_stable_window_not_met"],
            "artifact_before_process_start": True,
            "generated_at": "2026-04-24T10:00:00-04:00",
            "process_start_time": "2026-04-24T10:30:00-04:00",
            "artifact_path": "/tmp/paper_ib_restart_latest.json",
        }
    )

    failure_keys = {item["issue_key"] for item in failures}
    assert "ib_restart_drill_artifact_before_process_start" in failure_keys
    assert "ib_restart_drill_no_go" in failure_keys


def test_ib_restart_drill_check_ignores_stale_optional_prior_day_artifact() -> None:
    failures = watchdog._ib_restart_drill_check(
        {
            "required": False,
            "artifact_present": True,
            "artifact_day": "2026-05-07",
            "expected_day": "2026-05-08",
            "go_no_go": "no_go",
            "blockers": ["post_resume_stable_window_not_met"],
            "artifact_before_process_start": True,
            "generated_at": "2026-05-07T19:45:00-04:00",
            "process_start_time": "2026-05-08T09:00:00-04:00",
            "artifact_path": "/tmp/paper_ib_restart_latest.json",
        }
    )

    assert failures == []


def test_ib_restart_drill_check_blocks_missing_generated_at() -> None:
    failures = watchdog._ib_restart_drill_check(
        {
            "required": True,
            "artifact_present": True,
            "artifact_day": "2026-04-24",
            "expected_day": "2026-04-24",
            "go_no_go": "go",
            "blockers": [],
            "generated_at_missing": True,
            "process_start_time": "2026-04-24T10:30:00-04:00",
            "artifact_path": "/tmp/paper_ib_restart_latest.json",
        }
    )

    assert "ib_restart_drill_artifact_generated_at_missing" in {item["issue_key"] for item in failures}


def test_ib_restart_drill_status_uses_created_at_for_legacy_artifact(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        {
            "ts": "2026-04-24T10:10:00-04:00",
            "component": "market_data_service",
            "stage": "ib_disconnected",
            "status": "disconnected",
        },
    )
    _write(
        tmp_path / "runtime/readiness/drills/paper_ib_restart_20260424_latest.json",
        {
            "artifact_type": "paper_ib_restart_drill",
            "day": "2026-04-24",
            "created_at": "2026-04-24T10:00:00-04:00",
            "go_no_go": "go",
            "blockers": [],
        },
    )

    status = watchdog._ib_restart_drill_status(
        root=tmp_path,
        day_id="20260424",
        audit_dir=tmp_path / "runtime/audit/2026-04-24",
        process_start_time="2026-04-24T10:30:00-04:00",
    )

    assert status["generated_at"] == "2026-04-24T10:00:00-04:00"
    assert status["generated_at_missing"] is False
    assert status["artifact_before_process_start"] is True
    assert status["required"] is False


def test_ib_restart_drill_status_requires_only_current_process_disconnect(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/audit/2026-04-24/system-09.jsonl",
        {
            "ts": "2026-04-24T09:45:00-04:00",
            "component": "market_data_service",
            "stage": "ib_disconnected",
            "status": "disconnected",
        },
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        {
            "ts": "2026-04-24T10:10:00-04:00",
            "component": "market_data_service",
            "stage": "ib_disconnected",
            "status": "disconnected",
        },
    )

    old_only = watchdog._ib_restart_drill_status(
        root=tmp_path,
        day_id="20260424",
        audit_dir=tmp_path / "runtime/audit/2026-04-24",
        process_start_time="2026-04-24T10:30:00-04:00",
    )
    current_process = watchdog._ib_restart_drill_status(
        root=tmp_path,
        day_id="20260424",
        audit_dir=tmp_path / "runtime/audit/2026-04-24",
        process_start_time="2026-04-24T10:00:00-04:00",
    )

    assert old_only["required"] is False
    assert current_process["required"] is True


def test_ib_restart_drill_status_ignores_startup_ib_ready(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        {
            "ts": "2026-04-24T10:10:00-04:00",
            "component": "system_health_controller",
            "stage": "health_transition",
            "status": "recovering",
            "reason": "startup_ib_ready",
            "payload": {"reason": "startup_ib_ready"},
        },
    )

    status = watchdog._ib_restart_drill_status(
        root=tmp_path,
        day_id="20260424",
        audit_dir=tmp_path / "runtime/audit/2026-04-24",
        process_start_time="2026-04-24T10:00:00-04:00",
    )

    assert status["required"] is False


def test_watchdog_flags_missing_telegram_configuration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:34:30-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:34:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T09:34:30-04:00", "stage": "gate_blocked"},
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 34, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "telegram_notifier_unavailable" in failure_keys
    assert payload["telegram"]["available"] is False


def test_watchdog_flags_platform_eod_no_go_after_cutoff(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    _write(tmp_path / "config.yml", {"notifications": {"enabled": False, "telegram": {"enabled": False}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T16:00:00-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T16:00:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-16.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T16:00:00-04:00", "stage": "gate_blocked"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-16.jsonl",
        ts="2026-04-24T16:00:00-04:00",
    )
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "no_go",
            "state": "MANUAL_REQUIRED",
            "verified_flat": False,
            "manual_lock_ids": ["platform_eod_manual_lock:2026-04-24"],
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 16, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "platform_eod_no_go" in failure_keys
    assert "platform_eod_manual_lock_active" in failure_keys
    assert payload["recommendation"] == "HOLD"


def test_platform_eod_check_reports_awaiting_market_reopen(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "no_go",
            "state": "AWAITING_MARKET_REOPEN",
            "verified_flat": False,
            "awaiting_market_reopen": True,
            "awaiting_market_reopen_not_before": "2026-04-25T09:30:00-04:00",
            "recovery_incident_ids": ["recovery_incident:test:OPT:NVDA:20260424:100:P"],
            "manual_lock_ids": ["platform_eod_manual_lock:2026-04-24"],
        },
    )

    failures = watchdog._platform_eod_check(
        root=tmp_path,
        day_id="20260424",
        now_et=datetime(2026, 4, 24, 16, 0, tzinfo=watchdog.ET),
    )
    by_key = {item["issue_key"]: item for item in failures}

    assert by_key["platform_eod_no_go"]["error_type"] == "platform_eod_awaiting_market_reopen"
    assert by_key["platform_eod_no_go"]["awaiting_market_reopen_not_before"] == "2026-04-25T09:30:00-04:00"
    assert by_key["platform_eod_manual_lock_active"]["clear_condition"]


def test_platform_eod_awaiting_reconciles_with_current_flat_truth(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "no_go",
            "state": "AWAITING_MARKET_REOPEN",
            "verified_flat": False,
            "awaiting_market_reopen": True,
            "awaiting_market_reopen_not_before": "2026-04-25T09:30:00-04:00",
            "recovery_incident_ids": ["recovery_incident:test:OPT:NVDA:20260424:100:P"],
            "manual_lock_ids": ["platform_eod_manual_lock:2026-04-24"],
        },
    )
    _write(
        tmp_path / "runtime/readiness/manual_locks/platform_eod_manual_lock_2026-04-24.json",
        {
            "lock_id": "platform_eod_manual_lock:2026-04-24",
            "state": "ACTIVE",
            "reason": "manual_flatten_awaiting_option_market_reopen",
        },
    )
    now_et = datetime(2026, 4, 24, 22, 0, tzinfo=watchdog.ET)
    status = watchdog._platform_eod_status(root=tmp_path, day_id="20260424", now_et=now_et)

    reconciled = watchdog._reconcile_platform_eod_with_current_flat_truth(
        status=status,
        position_safety={
            "go_no_go": "go",
            "stale": False,
            "lookup_failed": False,
            "broker_position_count": 0,
            "active_quarantine_count": 0,
        },
        recovery_incidents={"active_count": 0},
        manual_locks={"lookup_failed": False, "active_count": 0, "locks": []},
        now_et=now_et,
    )

    assert reconciled["go_no_go"] == "go"
    assert reconciled["state"] == "VERIFIED_FLAT_BY_CURRENT_BROKER_TRUTH"
    assert reconciled["verified_flat"] is True
    assert reconciled["resolved_from_current_broker_truth"] is True
    assert reconciled["source_state"] == "AWAITING_MARKET_REOPEN"
    assert reconciled["manual_locks"] == []
    assert watchdog._platform_eod_failures(reconciled) == []


def test_platform_eod_awaiting_not_reconciled_with_active_current_manual_lock(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "no_go",
            "state": "AWAITING_MARKET_REOPEN",
            "verified_flat": False,
            "awaiting_market_reopen": True,
            "manual_lock_ids": ["platform_eod_manual_lock:2026-04-24"],
        },
    )
    now_et = datetime(2026, 4, 24, 22, 0, tzinfo=watchdog.ET)
    status = watchdog._platform_eod_status(root=tmp_path, day_id="20260424", now_et=now_et)

    reconciled = watchdog._reconcile_platform_eod_with_current_flat_truth(
        status=status,
        position_safety={
            "go_no_go": "go",
            "stale": False,
            "lookup_failed": False,
            "broker_position_count": 0,
            "active_quarantine_count": 0,
        },
        recovery_incidents={"active_count": 0},
        manual_locks={"lookup_failed": False, "active_count": 1, "locks": [{"lock_id": "current"}]},
        now_et=now_et,
    )

    assert reconciled["state"] == "AWAITING_MARKET_REOPEN"
    assert {item["issue_key"] for item in watchdog._platform_eod_failures(reconciled)} == {
        "platform_eod_no_go",
        "platform_eod_manual_lock_active",
    }


def test_platform_eod_awaiting_reconciles_target_scope_while_unrelated_extended_position_open(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "no_go",
            "state": "AWAITING_MARKET_REOPEN",
            "verified_flat": False,
            "awaiting_market_reopen": True,
            "recovery_incident_ids": ["recovery_incident:qqq_intraday_option_v1_001:OPT:QQQ:20260424:712:C"],
            "manual_lock_ids": [],
            "order_results": [
                {
                    "strategy_id": "qqq_intraday_option_v1_001",
                    "strategy_instance_id": "qqq_intraday_option_v1_001",
                    "symbol": "OPT:QQQ:20260424:712:C",
                    "success": False,
                    "reason": "manual_flatten_awaiting_option_market_reopen",
                }
            ],
        },
    )
    now_et = datetime(2026, 4, 24, 22, 0, tzinfo=watchdog.ET)
    status = watchdog._platform_eod_status(root=tmp_path, day_id="20260424", now_et=now_et)

    reconciled = watchdog._reconcile_platform_eod_with_current_flat_truth(
        status=status,
        position_safety={
            "go_no_go": "no_go",
            "stale": False,
            "lookup_failed": False,
            "broker_position_count": 1,
            "active_quarantine_count": 0,
            "live_broker_snapshot": {
                "positions": [{"symbol": "NVDA", "qty": 1}],
                "open_orders": [{"symbol": "NVDA", "order_id": 1500}],
            },
        },
        recovery_incidents={
            "active_count": 1,
            "incidents": [
                {
                    "incident_id": "recovery_incident:qqq_intraday_option_v1_001:OPT:QQQ:20260424:712:C",
                    "symbol": "OPT:QQQ:20260424:712:C",
                    "terminal": True,
                    "active_quarantine": False,
                },
                {
                    "incident_id": "recovery_incident:nvda_minute_flip_paper_001:NVDA",
                    "symbol": "NVDA",
                    "terminal": False,
                    "active_quarantine": True,
                },
            ],
        },
        manual_locks={
            "lookup_failed": False,
            "active_count": 1,
            "locks": [{"lock_id": "manual_lock:nvda_minute_flip_paper_001:NVDA", "symbol": "NVDA"}],
        },
        now_et=now_et,
    )

    assert reconciled["go_no_go"] == "go"
    assert reconciled["state"] == "VERIFIED_FLAT_BY_CURRENT_BROKER_TRUTH"
    assert reconciled["verified_flat"] is True
    assert watchdog._platform_eod_failures(reconciled) == []


def test_platform_eod_awaiting_target_scope_not_reconciled_when_target_open_order_exists(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "no_go",
            "state": "AWAITING_MARKET_REOPEN",
            "verified_flat": False,
            "awaiting_market_reopen": True,
            "order_results": [{"symbol": "OPT:QQQ:20260424:712:C"}],
        },
    )
    now_et = datetime(2026, 4, 24, 22, 0, tzinfo=watchdog.ET)
    status = watchdog._platform_eod_status(root=tmp_path, day_id="20260424", now_et=now_et)

    reconciled = watchdog._reconcile_platform_eod_with_current_flat_truth(
        status=status,
        position_safety={
            "go_no_go": "go",
            "stale": False,
            "lookup_failed": False,
            "broker_position_count": 0,
            "active_quarantine_count": 0,
            "live_broker_snapshot": {
                "positions": [],
                "open_orders": [{"symbol": "OPT:QQQ:20260424:712:C", "order_id": 9001}],
            },
        },
        recovery_incidents={"active_count": 0, "incidents": []},
        manual_locks={"lookup_failed": False, "active_count": 0, "locks": []},
        now_et=now_et,
    )

    assert reconciled["state"] == "AWAITING_MARKET_REOPEN"
    assert {item["issue_key"] for item in watchdog._platform_eod_failures(reconciled)} == {"platform_eod_no_go"}


def test_platform_eod_awaiting_target_scope_not_reconciled_when_target_incident_active(tmp_path: Path) -> None:
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "no_go",
            "state": "AWAITING_MARKET_REOPEN",
            "verified_flat": False,
            "awaiting_market_reopen": True,
            "order_results": [{"symbol": "OPT:QQQ:20260424:712:C"}],
        },
    )
    now_et = datetime(2026, 4, 24, 22, 0, tzinfo=watchdog.ET)
    status = watchdog._platform_eod_status(root=tmp_path, day_id="20260424", now_et=now_et)

    reconciled = watchdog._reconcile_platform_eod_with_current_flat_truth(
        status=status,
        position_safety={
            "go_no_go": "go",
            "stale": False,
            "lookup_failed": False,
            "broker_position_count": 0,
            "active_quarantine_count": 0,
            "live_broker_snapshot": {"positions": [], "open_orders": []},
        },
        recovery_incidents={
            "active_count": 1,
            "incidents": [
                {
                    "incident_id": "recovery_incident:qqq_intraday_option_v1_001:OPT:QQQ:20260424:712:C",
                    "symbol": "OPT:QQQ:20260424:712:C",
                    "terminal": False,
                    "active_quarantine": True,
                }
            ],
        },
        manual_locks={"lookup_failed": False, "active_count": 0, "locks": []},
        now_et=now_et,
    )

    assert reconciled["state"] == "AWAITING_MARKET_REOPEN"
    assert {item["issue_key"] for item in watchdog._platform_eod_failures(reconciled)} == {"platform_eod_no_go"}


def test_watchdog_flags_session_end_no_go_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    _write(
        tmp_path / "config.yml",
        {
            "notifications": {"enabled": False, "telegram": {"enabled": False}},
            "production": {"session_end_supervisor": {"enabled": True}},
        },
    )
    _write(
        tmp_path / "runtime/readiness/session_end/session_end_latest.json",
        {
            "artifact_type": "session_end_no_overnight",
            "generated_at": "2026-04-24T16:00:00-04:00",
            "go_no_go": "no_go",
            "lookup_failed": False,
            "violation_count": 1,
            "manual_required_count": 1,
            "violations": [
                {
                    "strategy_id": "qqq_intraday_001",
                    "symbol": "TQQQ",
                    "qty": 10.0,
                    "manual_required": True,
                    "manual_lock_ids": ["session_end_manual_lock:2026-04-24:qqq_intraday_001:TQQQ"],
                    "recovery_incident_id": "recovery_incident:qqq_intraday_001:TQQQ",
                }
            ],
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 16, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "session_end_no_go" in failure_keys
    assert "session_end_no_overnight_position" in failure_keys
    assert "session_end_manual_required" in failure_keys
    assert payload["session_end"]["violation_count"] == 1
    assert payload["recommendation"] == "HOLD"


def test_watchdog_ignores_session_end_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    _write(
        tmp_path / "config.yml",
        {
            "notifications": {"enabled": False, "telegram": {"enabled": False}},
            "production": {"session_end_supervisor": {"enabled": False}},
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 16, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "session_end_artifact_missing" not in failure_keys
    assert payload["session_end"]["enabled"] is False


def test_watchdog_flags_active_db_manual_intervention_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(
        watchdog,
        "_postgres_manual_lock_status",
        lambda *, config: {
            "enabled": True,
            "lookup_failed": False,
            "active_count": 1,
            "locks": [{"strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "reason": "platform_eod_manual_required"}],
        },
    )
    _write(
        tmp_path / "config.yml",
        {
            "notifications": {"enabled": False, "telegram": {"enabled": False}},
            "operations": {"runtime_watchdog": {"check_db_manual_locks": True}},
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T10:00:00-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T10:00:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-10.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T10:00:00-04:00", "stage": "gate_blocked"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        ts="2026-04-24T10:00:00-04:00",
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 10, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "manual_intervention_lock_active" in failure_keys
    assert payload["manual_intervention_locks"]["active_count"] == 1
    assert payload["recommendation"] == "HOLD"


def test_postgres_manual_lock_check_reports_awaiting_market_reopen() -> None:
    failures = watchdog._postgres_manual_lock_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "active_count": 1,
            "locks": [
                {
                    "strategy_id": "platform_position_safety",
                    "symbol": "OPT:NVDA:20260506:207.5:P",
                    "reason": "position_safety_manual_required",
                    "context": {"awaiting_market_reopen_not_before": "2026-05-07T09:30:00-04:00"},
                }
            ],
        }
    )

    assert failures[0]["error_type"] == "manual_intervention_lock_awaiting_market_reopen"
    assert failures[0]["awaiting_market_reopen_not_before"] == "2026-05-07T09:30:00-04:00"


def test_postgres_manual_lock_check_reports_partial_awaiting_market_reopen_context() -> None:
    failures = watchdog._postgres_manual_lock_check(
        {
            "enabled": True,
            "lookup_failed": False,
            "active_count": 2,
            "locks": [
                {
                    "strategy_id": "platform_position_safety",
                    "symbol": "OPT:NVDA:20260506:207.5:P",
                    "reason": "position_safety_manual_required",
                    "context": {"awaiting_market_reopen_not_before": "2026-05-07T09:30:00-04:00"},
                },
                {
                    "strategy_id": "nvda_option_churn_smoke_001",
                    "symbol": "OPT:NVDA:20260506:207.5:P",
                    "reason": "strategy_recovery_action_quarantine",
                    "context": {},
                },
            ],
        }
    )

    assert failures[0]["error_type"] == "manual_intervention_lock_active"
    assert failures[0]["awaiting_market_reopen_count"] == 1
    assert failures[0]["manual_lock_without_awaiting_context_count"] == 1


def test_manual_lock_annotation_infers_awaiting_context_from_recovery_incidents() -> None:
    status = watchdog._annotate_manual_locks_with_recovery_incident_context(
        {
            "enabled": True,
            "lookup_failed": False,
            "active_count": 2,
            "locks": [
                {
                    "strategy_id": "platform_position_safety",
                    "symbol": "OPT:NVDA:20260506:207.5:P",
                    "reason": "position_safety_manual_required",
                    "context": {"awaiting_market_reopen_not_before": "2026-05-07T09:30:00-04:00"},
                },
                {
                    "strategy_id": "nvda_option_churn_smoke_001",
                    "symbol": "OPT:NVDA:20260506:207.5:P",
                    "reason": "strategy_recovery_action_quarantine",
                    "context": {},
                },
            ],
        },
        recovery_incidents={
            "incidents": [
                {
                    "incident_id": "recovery_incident:nvda_option_churn_smoke_001:OPT:NVDA:20260506:207.5:P",
                    "symbol": "OPT:NVDA:20260506:207.5:P",
                    "state": "awaiting_market_reopen",
                    "active_recovery_intent": {
                        "next_action": "wait_until_market_reopen",
                        "next_retry_after": "2026-05-07T09:30:00-04:00",
                    },
                }
            ]
        },
    )

    failures = watchdog._postgres_manual_lock_check(status)

    assert status["awaiting_market_reopen_context_inferred"] is True
    assert failures[0]["error_type"] == "manual_intervention_lock_awaiting_market_reopen"
    assert failures[0]["awaiting_market_reopen_count"] == 2
    inferred_context = status["locks"][1]["context"]
    assert inferred_context["awaiting_market_reopen_context_source"] == "recovery_incidents"
    assert inferred_context["awaiting_market_reopen_recovery_incident_ids"] == [
        "recovery_incident:nvda_option_churn_smoke_001:OPT:NVDA:20260506:207.5:P"
    ]


def test_watchdog_flags_db_manual_intervention_lock_lookup_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(
        watchdog,
        "_postgres_manual_lock_status",
        lambda *, config: {
            "enabled": True,
            "lookup_failed": True,
            "active_count": 0,
            "locks": [],
            "error": "pg down",
        },
    )
    _write(
        tmp_path / "config.yml",
        {
            "notifications": {"enabled": False, "telegram": {"enabled": False}},
            "operations": {"runtime_watchdog": {"check_db_manual_locks": True}},
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T10:00:00-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T10:00:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-10.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T10:00:00-04:00", "stage": "gate_blocked"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        ts="2026-04-24T10:00:00-04:00",
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 10, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "manual_intervention_lock_lookup_failed" in failure_keys
    assert payload["recommendation"] == "HOLD"


def test_watchdog_flags_position_safety_no_go(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(
        watchdog,
        "_postgres_manual_lock_status",
        lambda *, config: {"enabled": False, "lookup_failed": False, "active_count": 0, "locks": []},
    )
    _write(tmp_path / "config.yml", {"notifications": {"enabled": False, "telegram": {"enabled": False}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T10:00:00-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T10:00:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-10.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T10:00:00-04:00", "stage": "gate_blocked"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        ts="2026-04-24T10:00:00-04:00",
    )
    _write(
        tmp_path / "runtime/readiness/position_safety/position_safety_latest.json",
        {
            "artifact_type": "position_safety",
            "generated_at": "2026-04-24T10:00:00-04:00",
            "go_no_go": "no_go",
            "lookup_failed": False,
            "broker_position_count": 1,
            "unsafe_position_count": 1,
            "records": [{"symbol": "TQQQ", "qty": 10, "classified_as": "unclassified"}],
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 10, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "position_safety_no_go" in failure_keys
    assert "position_safety_unsafe_position" in failure_keys
    assert payload["recommendation"] == "HOLD"


def test_watchdog_flags_missing_position_safety_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(
        watchdog,
        "_postgres_manual_lock_status",
        lambda *, config: {"enabled": False, "lookup_failed": False, "active_count": 0, "locks": []},
    )
    _write(tmp_path / "config.yml", {"notifications": {"enabled": False, "telegram": {"enabled": False}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T10:00:00-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T10:00:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-10.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T10:00:00-04:00", "stage": "gate_blocked"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        ts="2026-04-24T10:00:00-04:00",
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 10, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "position_safety_artifact_missing" in failure_keys
    assert payload["recommendation"] == "HOLD"


def test_watchdog_flags_position_safety_missing_after_close(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(
        watchdog,
        "_postgres_manual_lock_status",
        lambda *, config: {"enabled": False, "lookup_failed": False, "active_count": 0, "locks": []},
    )
    _write(tmp_path / "config.yml", {"notifications": {"enabled": False, "telegram": {"enabled": False}}})
    _write(
        tmp_path / "runtime/readiness/eod/platform_eod_flatten_2026-04-24.json",
        {
            "artifact_type": "platform_eod_flatten",
            "go_no_go": "go",
            "state": "VERIFIED_FLAT",
            "verified_flat": True,
            "manual_lock_ids": [],
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 16, 10, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "position_safety_artifact_missing" in failure_keys
    assert payload["recommendation"] == "HOLD"


def test_watchdog_flags_stale_position_safety_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(
        watchdog,
        "_postgres_manual_lock_status",
        lambda *, config: {"enabled": False, "lookup_failed": False, "active_count": 0, "locks": []},
    )
    _write(tmp_path / "config.yml", {"notifications": {"enabled": False, "telegram": {"enabled": False}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T10:00:00-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T10:00:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-10.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T10:00:00-04:00", "stage": "gate_blocked"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        ts="2026-04-24T10:00:00-04:00",
    )
    _write(
        tmp_path / "runtime/readiness/position_safety/position_safety_latest.json",
        {
            "artifact_type": "position_safety",
            "generated_at": "2026-04-24T09:55:00-04:00",
            "go_no_go": "go",
            "lookup_failed": False,
            "broker_position_count": 0,
            "unsafe_position_count": 0,
            "records": [],
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 10, 0, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "position_safety_artifact_stale" in failure_keys
    assert payload["recommendation"] == "HOLD"


def test_watchdog_loads_telegram_from_repo_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    _write(tmp_path / ".env", "TELEGRAM_BOT_TOKEN=bot\nTELEGRAM_CHAT_ID=chat\n")
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:34:30-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:34:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T09:34:30-04:00", "stage": "gate_blocked"},
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 34, tzinfo=watchdog.ET),
    )

    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert "telegram_notifier_unavailable" not in failure_keys
    assert payload["telegram"]["available"] is True


def test_watchdog_resends_open_alert_after_notifier_recovers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    state_path = tmp_path / "runtime/state/watchdog_alert_state.json"
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:34:30-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:34:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T09:34:30-04:00", "stage": "gate_blocked"},
    )

    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    first = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
        state_path=state_path,
    )
    assert any(item["issue_key"] == "opening_artifact_missing:preopen" for item in first["failures"])

    manager = _FakeManager()
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: manager)
    _write(tmp_path / ".env", "TELEGRAM_BOT_TOKEN=bot\nTELEGRAM_CHAT_ID=chat\n")
    second = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 46, tzinfo=watchdog.ET),
        state_path=state_path,
    )

    assert any(event["status"] == "ERROR" and event["metadata"].get("issue_key") == "opening_artifact_missing:preopen" for event in manager.events)
    issue_state = (second.get("transition_state") or {}).get("issues", {}).get("opening_artifact_missing:preopen", {})
    assert issue_state.get("alert_open_sent") is True


def test_watchdog_reports_opening_artifact_presence_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}}})
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:44:30-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T09:44:30-04:00", "stage": "gate_blocked"},
    )
    _write(tmp_path / "runtime/state/opening_day/20260424_preopen_supervisor.json", {"final": {"phase": "preopen"}})
    _write(tmp_path / "runtime/state/opening_day/20260424_open_supervisor.json", {"final": {"phase": "open"}})
    _write(tmp_path / "runtime/state/opening_day/20260424_acceptance_supervisor.json", {"final": {"phase": "acceptance"}})

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
    )

    assert payload["opening_artifacts"]["checked_phases"] == ["preopen", "open", "acceptance"]
    assert payload["opening_artifacts"]["present_phases"] == ["preopen", "open", "acceptance"]


def test_watchdog_reports_alive_but_not_tradable_qqq_liveness(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    _write(
        tmp_path / "config.yml",
        {
            "notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}},
            "strategies": [
                {
                    "id": "qqq_intraday_001",
                    "enabled": True,
                    "params": {"extras": {"data_contract": {"symbols": ["QQQ"], "datasets": ["ticks"]}}},
                }
            ],
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {
            "ts_now_et": "2026-04-24T09:47:00-04:00",
            "strategy_id": "qqq_intraday_001",
            "ready": False,
            "missing_required": ["QQQ:last_stale"],
            "required_values": {"QQQ": {"age_sec": 50521.0}},
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:47:00-04:00","event":"qqq_loop_heartbeat","loop_last_stage":"system_paused","stream_invalid":true,"data_age_qqq_sec":50521.0}\n',
    )
    _write(tmp_path / "runtime/state/opening_day/20260424_preopen_supervisor.json", {"final": {"phase": "preopen"}})
    _write(tmp_path / "runtime/state/opening_day/20260424_open_supervisor.json", {"final": {"phase": "open"}})
    _write(tmp_path / "runtime/state/opening_day/20260424_acceptance_supervisor.json", {"final": {"phase": "acceptance"}})

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 47, tzinfo=watchdog.ET),
    )

    assert payload["qqq_liveness"]["alive"] is True
    assert payload["qqq_liveness"]["tradable"] is False
    assert payload["qqq_liveness"]["tradable_entry_ready"] is False
    assert payload["qqq_liveness"]["data_ready"] is False
    assert payload["qqq_liveness"]["system_ready"] is False
    assert payload["qqq_liveness"]["data_blockers"] == [
        "not_ready",
        "missing_required:QQQ:last_stale",
        "stream_invalid",
        "qqq_market_data_stale",
        "runtime_audit_missing",
    ]
    assert payload["qqq_liveness"]["system_blockers"] == ["system_paused"]
    assert payload["qqq_liveness"]["blocking_reasons"] == [
        "not_ready",
        "missing_required:QQQ:last_stale",
        "stream_invalid",
        "system_paused",
        "qqq_market_data_stale",
        "runtime_audit_missing",
    ]
    assert payload["opening_artifacts"]["missing_phases"] == []
    assert payload["opening_artifacts"]["invalid_phases"] == []
    failure_keys = {item["issue_key"] for item in payload["failures"]}
    assert any(key.startswith("strategy_tradability:qqq_intraday_001:") for key in failure_keys)
    assert payload["strategy_tradability"]["go_no_go"] == "no_go"
    assert payload["production_state"]["go_no_go"] == "no_go"


def test_watchdog_reports_stale_run_system_lock_when_process_is_alive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: pid == 111)
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}}})
    _write(tmp_path / "runtime/locks/run_system.lock", "222\n")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-24T09:44:30-04:00", "ready": True},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-24T09:44:30-04:00", "stage": "gate_blocked"},
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 34, tzinfo=watchdog.ET),
    )

    assert payload["run_system"]["process_pids"] == [111]
    assert payload["run_system"]["alive"] is True
    assert payload["run_system"]["lock_pid"] == 222
    assert payload["run_system"]["lock_pid_alive"] is False
    assert payload["run_system"]["lock_matches_process"] is False
    assert payload["run_system"]["stale_lock"] is True


def test_watchdog_accepts_option_live_runtime_audit_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}}})
    _write(tmp_path / "runtime/locks/run_system.lock", "111\n")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {
            "ts_now_et": "2026-04-24T09:44:30-04:00",
            "ready": True,
            "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}},
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_option_v1_001", "ts": "2026-04-24T09:44:30-04:00", "stage": "feature_callback_heartbeat"},
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
    )

    assert payload["qqq_runtime_audit"]["row_count"] == 1
    assert payload["qqq_runtime_audit"]["matched_strategy_ids"] == ["qqq_intraday_option_v1_001"]
    assert "qqq_runtime_audit_missing" not in {row["issue_key"] for row in payload["failures"]}


def test_watchdog_fails_stale_system_health_transition(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}}})
    _write(tmp_path / "runtime/locks/run_system.lock", "111\n")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {
            "ts_now_et": "2026-04-24T09:44:30-04:00",
            "ready": True,
            "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}},
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-24T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_option_v1_001", "ts": "2026-04-24T09:44:30-04:00", "stage": "feature_callback_heartbeat"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-09.jsonl",
        ts="2026-04-24T09:00:00-04:00",
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 9, 45, tzinfo=watchdog.ET),
    )

    assert "system_health_transition_stale" in {row["issue_key"] for row in payload["failures"]}
    assert payload["recommendation"] == "HOLD"


def test_watchdog_fails_stale_system_health_transition_after_close(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(watchdog, "_opening_artifact_check", lambda **kwargs: [])
    monkeypatch.setattr(watchdog, "_platform_eod_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_postgres_manual_lock_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_position_safety_check", lambda *args, **kwargs: [])
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}}})
    _write(tmp_path / "runtime/locks/run_system.lock", "111\n")
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-15.jsonl",
        ts="2026-04-24T15:55:00-04:00",
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 16, 30, tzinfo=watchdog.ET),
    )

    assert "system_health_transition_stale" in {row["issue_key"] for row in payload["failures"]}
    assert payload["recommendation"] == "HOLD"


def test_watchdog_requires_ib_restart_drill_when_disconnect_seen(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(watchdog, "_opening_artifact_check", lambda **kwargs: [])
    monkeypatch.setattr(watchdog, "_platform_eod_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_postgres_manual_lock_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_position_safety_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_system_health_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        watchdog,
        "_runtime_version_status",
        lambda root: {
            "process_pid": 111,
            "process_start_time": "2026-04-24T10:00:00-04:00",
            "loaded_code_current": True,
            "stale_modules": [],
        },
    )
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}}})
    _write(tmp_path / "runtime/locks/run_system.lock", "111\n")
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-09.jsonl",
        ts="2026-04-24T09:45:00-04:00",
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        {
            "ts": "2026-04-24T10:10:00-04:00",
            "component": "market_data_service",
            "stage": "ib_disconnected",
            "status": "disconnected",
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 16, 30, tzinfo=watchdog.ET),
    )

    failure_keys = {row["issue_key"] for row in payload["failures"]}
    assert "ib_restart_drill_artifact_missing" in failure_keys
    assert payload["recommendation"] == "HOLD"


def test_watchdog_accepts_go_ib_restart_drill_when_disconnect_seen(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)
    monkeypatch.setattr(watchdog, "_find_run_system_pids", lambda root: [111])
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watchdog, "build_runtime_reconciliation_health", lambda **kwargs: {"blockers": []})
    monkeypatch.setattr(watchdog, "_opening_artifact_check", lambda **kwargs: [])
    monkeypatch.setattr(watchdog, "_platform_eod_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_postgres_manual_lock_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_position_safety_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(watchdog, "_system_health_check", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        watchdog,
        "_runtime_version_status",
        lambda root: {
            "process_pid": 111,
            "process_start_time": "2026-04-24T10:00:00-04:00",
            "loaded_code_current": True,
            "stale_modules": [],
        },
    )
    _write(tmp_path / "config.yml", {"notifications": {"enabled": True, "telegram": {"enabled": True, "bot_token": "bot", "chat_id": "chat"}}})
    _write(tmp_path / "runtime/locks/run_system.lock", "111\n")
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-24/system-09.jsonl",
        ts="2026-04-24T09:45:00-04:00",
    )
    _write(
        tmp_path / "runtime/audit/2026-04-24/system-10.jsonl",
        {
            "ts": "2026-04-24T10:10:00-04:00",
            "component": "market_data_service",
            "stage": "ib_disconnected",
            "status": "disconnected",
        },
    )
    _write(
        tmp_path / "runtime/readiness/drills/paper_ib_restart_20260424_latest.json",
        {
            "artifact_type": "paper_ib_restart_drill",
            "day": "2026-04-24",
            "generated_at": "2026-04-24T10:30:00-04:00",
            "go_no_go": "go",
            "blockers": [],
            "timeline": [{"kind": "system_resumed"}],
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260424",
        now_et=datetime(2026, 4, 24, 16, 30, tzinfo=watchdog.ET),
    )

    failure_keys = {row["issue_key"] for row in payload["failures"]}
    assert "ib_restart_drill_artifact_missing" not in failure_keys
    assert "ib_restart_drill_no_go" not in failure_keys
    assert payload["ib_restart_drill"]["go_no_go"] == "go"


def test_watchdog_skips_non_trading_day(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(watchdog, "is_trading_day", lambda d: False)
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: None)

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260425",
        now_et=datetime(2026, 4, 25, 9, 45, tzinfo=watchdog.ET),
    )

    assert payload["recommendation"] == "PASS"
    assert payload["failure_count"] == 0
    assert payload["market_calendar"]["trading_day"] is False
    assert (payload.get("transition_state") or {}).get("status") == "non_trading_day_no_action"


def test_watchdog_non_trading_day_does_not_emit_recovery_alerts(tmp_path: Path, monkeypatch) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(watchdog, "is_trading_day", lambda d: False)
    monkeypatch.setattr(watchdog, "build_notification_manager", lambda config: manager)
    state_path = tmp_path / "runtime/state/watchdog_alert_state.json"
    _write(
        state_path,
        {
            "day": "20260424",
            "issues": {
                "opening_artifact_missing:preopen": {
                    "status": "open",
                    "opened_at": "2026-04-24T09:45:00-04:00",
                }
            },
        },
    )

    payload = watchdog.run_runtime_operations_watchdog(
        repo_root=tmp_path,
        day="20260425",
        now_et=datetime(2026, 4, 25, 9, 45, tzinfo=watchdog.ET),
        state_path=state_path,
    )

    assert payload["failure_count"] == 0
    assert manager.events == []


def test_operator_proof_visibility_in_production_state(tmp_path: Path) -> None:
    generated_at = "2026-05-12T10:00:00-04:00"
    _write(
        tmp_path / "runtime/recovery/ib_gateway_operator_proof/latest.json",
        {
            "generated_at": generated_at,
            "source": "operator_cli",
            "result": "SUCCESS",
            "manual_required_cleared": True,
        },
    )

    proof = watchdog._ib_gateway_operator_proof_status(
        root=tmp_path,
        now_et=datetime(2026, 5, 12, 10, 0, 5, tzinfo=watchdog.ET),
    )
    state = watchdog._build_production_state(
        {
            "generated_at": "2026-05-12T10:00:05-04:00",
            "day": "20260512",
            "failures": [],
            "ib_gateway_operator_proof": proof,
            "runtime_version": {"loaded_code_current": True},
        }
    )

    visible = state["readiness_details"]["ib_gateway_operator_proof"]
    assert visible["latest_result"] == "SUCCESS"
    assert visible["generated_at"] == generated_at
    assert visible["source"] == "operator_cli"
    assert visible["manual_required_cleared"] is True
    assert visible["age_sec"] == 5.0
