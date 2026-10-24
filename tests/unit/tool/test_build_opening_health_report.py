from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import tool.ops.build_opening_health_report as opening_health


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


def _write_production_state(path: Path, *, ts: str, go_no_go: str = "go", failures: list[dict] | None = None) -> None:
    failure_items = list(failures or [])
    _write(
        path,
        {
            "artifact_type": "production_state",
            "generated_at": ts,
            "go_no_go": go_no_go,
            "recommendation": "PASS" if go_no_go == "go" and not failure_items else "HOLD",
            "failure_count": len(failure_items),
            "failures": failure_items,
            "system_health": {"state": "running", "present": True},
            "position_safety": {"go_no_go": "go"},
            "platform_eod": {"go_no_go": "go", "verified_flat": True},
        },
    )


def test_opening_health_report_flags_missing_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [])
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-22T09:30:10-04:00", "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": "2026-04-22T09:30:10-04:00"},
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert report["recommendation"] == "AUTO_REMEDIATE"
    assert "run_system_down" in report["blockers"]
    assert "start_run_system" in report["remediation_actions"]


def test_opening_health_report_acceptance_requires_trace_coverage(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 4, 22, 9, 45, 0, tzinfo=opening_health.ET)
    now_iso = now.isoformat(timespec="seconds")
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(opening_health, "datetime", _FakeDateTime)
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": now_iso, "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": now_iso},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-22T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-22/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-22T09:44:30-04:00", "stage": "gate_blocked"},
    )
    _write(
        tmp_path / "reports/qqq_signal_snapshots/qqq_signal_snapshots_20260422_qqq_kernel_legacy_on_001.jsonl",
        {"strategy_id": "qqq_kernel_legacy_on_001", "snapshot_kind": "evaluation"},
    )
    _write(
        tmp_path / "reports/qqq_signal_snapshots/qqq_signal_snapshots_20260422_qqq_kernel_legacy_off_001.jsonl",
        {"strategy_id": "qqq_kernel_legacy_off_001", "snapshot_kind": "evaluation"},
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="acceptance", day="20260422")

    assert report["recommendation"] == "HOLD"
    assert "trace_coverage:qqq_intraday_001" in report["blockers"]


def test_opening_health_report_ignores_event_driven_state_staleness(tmp_path: Path, monkeypatch) -> None:
    now_iso = datetime.now(opening_health.ET).isoformat(timespec="seconds")
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {
            "ts_now_et": now_iso,
            "ready": True,
            "required_values": {
                "QQQ": {"last": 1, "vwap": 1, "age_sec": 1},
                "TQQQ": {"last": 1, "age_sec": 1},
                "SQQQ": {"last": 1, "age_sec": 1},
            },
        },
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {
            "updated_at": "2026-04-22T09:00:00-04:00",
            "updated_at_semantics": "event_driven_state_snapshot",
        },
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert "qqq_state_stale" not in report["blockers"]
    assert report["qqq_state"]["requires_heartbeat"] is False


def test_opening_health_report_flags_stale_diag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    now = datetime(2026, 4, 22, 9, 35, 0, tzinfo=opening_health.ET)
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(opening_health, "datetime", _FakeDateTime)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-22T09:34:30-04:00", "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": "2026-04-22T09:34:30-04:00"},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-22T09:20:00-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-22/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_001", "ts": "2026-04-22T09:34:30-04:00", "stage": "gate_blocked"},
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert "qqq_diag_stale" in report["blockers"]
    assert report["qqq_diag"]["last_event"] == "qqq_loop_heartbeat"


def test_opening_health_report_blocks_stale_system_health(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    now = datetime(2026, 4, 22, 9, 45, 0, tzinfo=opening_health.ET)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(opening_health, "datetime", _FakeDateTime)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-22T09:44:30-04:00", "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": "2026-04-22T09:44:30-04:00"},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-22T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-22/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_option_v1_001", "ts": "2026-04-22T09:44:30-04:00", "stage": "feature_callback_heartbeat"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-22/system-09.jsonl",
        ts="2026-04-22T09:00:00-04:00",
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert "system_health:stale" in report["blockers"]
    assert report["recommendation"] == "HOLD"


def test_opening_health_report_blocks_missing_production_state_after_open_grace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    now = datetime(2026, 4, 22, 9, 45, 0, tzinfo=opening_health.ET)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(opening_health, "datetime", _FakeDateTime)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-22T09:44:30-04:00", "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": "2026-04-22T09:44:30-04:00"},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-22T09:44:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-22/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_option_v1_001", "ts": "2026-04-22T09:44:30-04:00", "stage": "feature_callback_heartbeat"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-22/system-09.jsonl",
        ts="2026-04-22T09:44:30-04:00",
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert "production_state_missing" in report["blockers"]
    assert report["recommendation"] == "HOLD"


def test_opening_health_report_accepts_fresh_go_production_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    now = datetime(2026, 4, 22, 9, 45, 0, tzinfo=opening_health.ET)
    now_iso = now.isoformat(timespec="seconds")

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(opening_health, "datetime", _FakeDateTime)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": now_iso, "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": now_iso},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        f'{{"ts_now_et":"{now_iso}","event":"qqq_loop_heartbeat"}}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-22/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_option_v1_001", "ts": now_iso, "stage": "feature_callback_heartbeat"},
    )
    _write_system_health(
        tmp_path / "runtime/audit/2026-04-22/system-09.jsonl",
        ts=now_iso,
    )
    _write_production_state(
        tmp_path / "runtime/readiness/production_state/production_state_latest.json",
        ts=now_iso,
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert "production_state_missing" not in report["blockers"]
    assert "production_state_stale" not in report["blockers"]
    assert "production_state_no_go" not in report["blockers"]
    assert report["production_state"]["go_no_go"] == "go"


def test_opening_health_report_flags_missing_runtime_audit_after_open_grace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    now = datetime(2026, 4, 22, 9, 35, 0, tzinfo=opening_health.ET)
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(opening_health, "datetime", _FakeDateTime)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-22T09:34:30-04:00", "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": "2026-04-22T09:34:30-04:00"},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-22T09:34:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-22/strategy-09.jsonl",
        {"strategy_id": "qqq_1dte_ma10_reject_0324_001", "ts": "2026-04-22T09:34:30-04:00", "stage": "feature_callback_heartbeat"},
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert "qqq_runtime_audit_missing" in report["blockers"]
    assert report["qqq_runtime_audit"]["row_count"] == 0


def test_opening_health_report_accepts_option_live_runtime_audit_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "_find_run_system_pids", lambda repo_root: [12345])
    monkeypatch.setattr(opening_health, "_pid_alive", lambda pid: True)
    now = datetime(2026, 4, 22, 9, 35, 0, tzinfo=opening_health.ET)

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(opening_health, "datetime", _FakeDateTime)
    _write(tmp_path / "runtime/locks/run_system.lock", "12345")
    _write(
        tmp_path / "runtime/state/qqq_intraday_observability_qqq_intraday_001.json",
        {"ts_now_et": "2026-04-22T09:34:30-04:00", "ready": True, "required_values": {"QQQ": {"last": 1, "vwap": 1, "age_sec": 1}, "TQQQ": {"last": 1, "age_sec": 1}, "SQQQ": {"last": 1, "age_sec": 1}}},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_qqq_intraday_001.json",
        {"updated_at": "2026-04-22T09:34:30-04:00"},
    )
    _write(
        tmp_path / "runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl",
        '{"ts_now_et":"2026-04-22T09:34:30-04:00","event":"qqq_loop_heartbeat"}\n',
    )
    _write(
        tmp_path / "runtime/audit/2026-04-22/strategy-09.jsonl",
        {"strategy_id": "qqq_intraday_option_v1_001", "ts": "2026-04-22T09:34:30-04:00", "stage": "feature_callback_heartbeat"},
    )

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260422")

    assert "qqq_runtime_audit_missing" not in report["blockers"]
    assert report["qqq_runtime_audit"]["row_count"] == 1
    assert report["qqq_runtime_audit"]["matched_strategy_ids"] == ["qqq_intraday_option_v1_001"]


def test_opening_health_report_skips_non_trading_day(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(opening_health, "is_trading_day", lambda d: False)

    report = opening_health.build_opening_health_report(repo_root=tmp_path, phase="open", day="20260425")

    assert report["recommendation"] == "PASS"
    assert report["blockers"] == []
    assert report["remediation_actions"] == []
    assert report["market_calendar"]["trading_day"] is False
