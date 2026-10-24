from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from tool.ops.system_health_artifact import ET, latest_system_health_transition


def _write_transition(path: Path, *, ts: datetime, status: str = "running") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": ts.isoformat(),
        "ts_et": ts.isoformat(),
        "domain": "system",
        "component": "system_health_controller",
        "stage": "health_transition",
        "status": status,
        "reason": "runtime_reconciliation_clean",
        "payload": {
            "state": status,
            "event_type": "system_resumed",
            "ts_et": ts.isoformat(),
            "metadata": {
                "platform_recovery_ready": True,
                "recovery_readiness": {"ready": True, "blockers": []},
            },
        },
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_latest_system_health_transition_marks_stale_running_unhealthy(tmp_path: Path) -> None:
    now = datetime(2026, 5, 4, 15, 26, tzinfo=ET)
    _write_transition(tmp_path / "system-11.jsonl", ts=now - timedelta(hours=4, minutes=10))

    result = latest_system_health_transition(audit_dir=tmp_path, now_et=now, max_age_sec=90)

    assert result["present"] is True
    assert result["state"] == "running"
    assert result["stale"] is True
    assert result["unhealthy"] is True
    assert result["stale_reason"] == "system_health_transition_stale"


def test_latest_system_health_transition_accepts_fresh_running(tmp_path: Path) -> None:
    now = datetime(2026, 5, 4, 15, 26, tzinfo=ET)
    _write_transition(tmp_path / "system-15.jsonl", ts=now - timedelta(seconds=30))

    result = latest_system_health_transition(audit_dir=tmp_path, now_et=now, max_age_sec=90)

    assert result["stale"] is False
    assert result["unhealthy"] is False
