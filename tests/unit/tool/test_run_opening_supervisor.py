from __future__ import annotations

from pathlib import Path

import tool.ops.run_opening_supervisor as opening_supervisor


def test_opening_supervisor_starts_missing_system(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        opening_supervisor,
        "build_opening_health_report",
        lambda **kwargs: {
            "phase": "open",
            "day": "20260422",
            "recommendation": "AUTO_REMEDIATE",
            "blockers": ["run_system_down"],
            "remediation_actions": ["start_run_system"],
            "run_system": {"process_pids": []},
            "qqq_observability": {"age_sec": None},
            "qqq_state": {"age_sec": None},
        },
    )
    script = tmp_path / "scripts/run_system_autostart.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\necho started\n", encoding="utf-8")
    script.chmod(0o755)

    payload = opening_supervisor.run_opening_supervisor(
        repo_root=tmp_path,
        phase="open",
        day="20260422",
        auto_remediate=True,
        restart_cmd="scripts/run_system_autostart.sh",
        recheck_delay_sec=0.0,
    )

    assert payload["initial"]["recommendation"] == "AUTO_REMEDIATE"
    assert payload["actions"][0]["action"] == "start_run_system"


def test_opening_supervisor_default_start_command_is_nonblocking(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        opening_supervisor,
        "build_opening_health_report",
        lambda **kwargs: {
            "phase": "open",
            "day": "20260422",
            "recommendation": "AUTO_REMEDIATE",
            "blockers": ["run_system_down"],
            "remediation_actions": ["start_run_system"],
            "run_system": {"process_pids": []},
            "qqq_observability": {"age_sec": None},
            "qqq_state": {"age_sec": None},
        },
    )
    commands: list[str] = []
    monkeypatch.setattr(
        opening_supervisor,
        "_run_restart_command",
        lambda cmd, cwd: commands.append(cmd) or {"returncode": 0, "stdout_tail": "", "stderr_tail": ""},
    )
    monkeypatch.setattr(opening_supervisor.os, "getuid", lambda: 501)

    payload = opening_supervisor.run_opening_supervisor(
        repo_root=tmp_path,
        phase="open",
        day="20260422",
        auto_remediate=True,
        recheck_delay_sec=0.0,
    )

    assert payload["actions"][0]["action"] == "start_run_system"
    assert commands == [
        "launchctl print gui/501/com.trading.run_system.0925 >/dev/null 2>&1 "
        "&& launchctl kickstart -k gui/501/com.trading.run_system.0925 "
        "|| nohup scripts/run_system_autostart.sh >/dev/null 2>&1 &"
    ]


def test_opening_supervisor_restarts_when_observability_is_stale(tmp_path: Path, monkeypatch) -> None:
    reports = iter(
        [
            {
                "phase": "open",
                "day": "20260422",
                "recommendation": "AUTO_REMEDIATE",
                "blockers": ["qqq_observability_stale"],
                "remediation_actions": ["restart_run_system"],
                "run_system": {"process_pids": [111, 222]},
                "qqq_observability": {"age_sec": 240},
                "qqq_state": {"age_sec": 10, "requires_heartbeat": False},
            },
            {
                "phase": "open",
                "day": "20260422",
                "recommendation": "PASS",
                "blockers": [],
                "remediation_actions": [],
                "run_system": {"process_pids": [333]},
                "qqq_observability": {"age_sec": 1},
                "qqq_state": {"age_sec": 1, "requires_heartbeat": False},
            },
        ]
    )
    monkeypatch.setattr(opening_supervisor, "build_opening_health_report", lambda **kwargs: next(reports))
    monkeypatch.setattr(
        opening_supervisor,
        "_terminate_pids",
        lambda pids: {"terminated_pids": list(pids), "terminate_failures": []},
    )
    monkeypatch.setattr(
        opening_supervisor,
        "_run_restart_command",
        lambda cmd, cwd: {"returncode": 0, "stdout_tail": "ok", "stderr_tail": ""},
    )

    payload = opening_supervisor.run_opening_supervisor(
        repo_root=tmp_path,
        phase="open",
        day="20260422",
        auto_remediate=True,
        recheck_delay_sec=0.0,
        stale_restart_sec=180.0,
    )

    assert [item["action"] for item in payload["actions"]] == [
        "terminate_stale_run_system",
        "restart_run_system",
    ]
    assert payload["final"]["recommendation"] == "PASS"


def test_opening_supervisor_marks_restart_command_failure(tmp_path: Path, monkeypatch) -> None:
    reports = iter(
        [
            {
                "phase": "open",
                "day": "20260422",
                "recommendation": "AUTO_REMEDIATE",
                "blockers": ["run_system_down"],
                "remediation_actions": ["start_run_system"],
                "run_system": {"process_pids": []},
                "qqq_observability": {"age_sec": None},
                "qqq_state": {"age_sec": None},
            },
            {
                "phase": "open",
                "day": "20260422",
                "recommendation": "AUTO_REMEDIATE",
                "blockers": ["run_system_down"],
                "remediation_actions": ["start_run_system"],
                "run_system": {"process_pids": []},
                "qqq_observability": {"age_sec": None},
                "qqq_state": {"age_sec": None},
            },
        ]
    )
    monkeypatch.setattr(opening_supervisor, "build_opening_health_report", lambda **kwargs: next(reports))
    monkeypatch.setattr(
        opening_supervisor,
        "_run_restart_command",
        lambda cmd, cwd: {"returncode": 1, "stdout_tail": "", "stderr_tail": "permission denied"},
    )

    payload = opening_supervisor.run_opening_supervisor(
        repo_root=tmp_path,
        phase="open",
        day="20260422",
        auto_remediate=True,
        recheck_delay_sec=0.0,
    )

    assert payload["actions"][0]["action"] == "start_run_system"
    assert "restart_command_failed" in payload["final"]["blockers"]
