from __future__ import annotations

import json
from pathlib import Path

from tool.ops.record_rollout_execution import record_rollout_execution


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_record_rollout_execution_updates_last_execution_and_attempts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tool.ops.record_rollout_execution._detect_git_commit",
        lambda: {"sha": "abc123", "summary": "fallback fix"},
    )
    state_path = tmp_path / "runtime/state/trading_rollout_orchestrator_state.json"
    _write_json(
        state_path,
        {
            "current_priority_task": {
                "id": "qqq_trace_coverage",
                "title": "Restore QQQ live+shadow trace coverage",
            }
        },
    )

    payload = record_rollout_execution(
        state_path=state_path,
        output_path=state_path,
        task_id="qqq_trace_coverage",
        status="blocked",
        summary="Waiting for next market session to verify legacy writer",
        blockers=["trace_coverage:qqq_intraday_001"],
    )

    assert payload["last_execution"]["task_id"] == "qqq_trace_coverage"
    assert payload["last_execution"]["status"] == "blocked"
    assert "commit" not in payload["last_execution"]
    assert payload["task_attempts"]["qqq_trace_coverage"]["attempt_count"] == 1
    assert payload["current_priority_task"]["last_attempt_status"] == "blocked"


def test_record_rollout_execution_tracks_last_completed_task_on_success(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime/state/trading_rollout_orchestrator_state.json"
    _write_json(state_path, {})

    payload = record_rollout_execution(
        state_path=state_path,
        output_path=state_path,
        task_id="refresh_reports",
        status="success",
        summary="Refreshed QQQ postclose and evidence reports",
    )

    assert payload["last_completed_task"]["task_id"] == "refresh_reports"
    assert payload["task_attempts"]["refresh_reports"]["last_status"] == "success"
