from __future__ import annotations

import json
from pathlib import Path

from tool.ops.update_rollout_orchestrator_state import update_rollout_orchestrator_state


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_orchestrator_state_picks_trace_coverage_as_priority_and_escalates_on_repeat(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "runtime/state/trading_rollout_state.json",
        {
            "last_day": "20260421",
            "last_recommendation": "CONTINUE_SHADOW",
            "paper_clean_days": 1,
            "min_paper_clean_days": 3,
            "qqq_shadow_observed_day_count": 0,
            "live_cutover_allowed": False,
        },
    )
    _write_json(tmp_path / "reports/daily_trading_evidence/20260421.json", {"day": "20260421", "recommendation": "CONTINUE_SHADOW", "blockers": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260421_preopen_smoke.json", {"blockers": []})
    _write_json(
        tmp_path / "reports/qqq_kernel_shadow_parity/20260421_trace_coverage.json",
        {"missing_strategy_ids": ["qqq_intraday_001"], "recommendation": "HOLD"},
    )
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260421_postclose_report.json", {"blockers": ["no_entry_candidate_today"]})
    output = tmp_path / "runtime/state/trading_rollout_orchestrator_state.json"

    first = update_rollout_orchestrator_state(
        repo_root=tmp_path,
        rollout_state_path=tmp_path / "runtime/state/trading_rollout_state.json",
        output_path=output,
        min_escalation_streak=2,
    )
    second = update_rollout_orchestrator_state(
        repo_root=tmp_path,
        rollout_state_path=tmp_path / "runtime/state/trading_rollout_state.json",
        output_path=output,
        min_escalation_streak=2,
    )

    assert first["current_priority_task"]["id"] == "qqq_trace_coverage"
    assert first["escalation_required"] is False
    assert second["escalation_required"] is True
    assert "trace_coverage:qqq_intraday_001" in second["escalation_blockers"]


def test_orchestrator_state_falls_back_to_evidence_blocker(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "runtime/state/trading_rollout_state.json",
        {
            "last_day": "20260421",
            "last_recommendation": "HOLD",
            "paper_clean_days": 0,
            "min_paper_clean_days": 3,
            "qqq_shadow_observed_day_count": 0,
            "live_cutover_allowed": False,
        },
    )
    _write_json(
        tmp_path / "reports/daily_trading_evidence/20260421.json",
        {"day": "20260421", "recommendation": "HOLD", "blockers": ["phase0:phase0_freeze_baseline_missing"]},
    )
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260421_preopen_smoke.json", {"blockers": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260421_trace_coverage.json", {"missing_strategy_ids": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260421_postclose_report.json", {"blockers": []})

    payload = update_rollout_orchestrator_state(
        repo_root=tmp_path,
        rollout_state_path=tmp_path / "runtime/state/trading_rollout_state.json",
        output_path=tmp_path / "runtime/state/trading_rollout_orchestrator_state.json",
    )

    assert payload["current_priority_task"]["id"] == "daily_evidence_blocker"
    assert payload["blockers"] == ["daily_evidence:phase0:phase0_freeze_baseline_missing"]


def test_orchestrator_state_includes_opening_severity_and_reports(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "runtime/state/trading_rollout_state.json",
        {
            "last_day": "20260422",
            "last_recommendation": "CONTINUE_SHADOW",
            "paper_clean_days": 1,
            "min_paper_clean_days": 3,
            "qqq_shadow_observed_day_count": 0,
            "live_cutover_allowed": False,
        },
    )
    _write_json(tmp_path / "reports/daily_trading_evidence/20260422.json", {"day": "20260422", "recommendation": "CONTINUE_SHADOW", "blockers": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260422_preopen_smoke.json", {"blockers": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260422_trace_coverage.json", {"missing_strategy_ids": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260422_postclose_report.json", {"blockers": []})
    _write_json(
        tmp_path / "runtime/state/opening_day/20260422_open_supervisor.json",
        {
            "initial": {"recommendation": "AUTO_REMEDIATE", "phase": "open", "day": "20260422"},
            "actions": [{"action": "restart_run_system"}],
            "final": {"recommendation": "PASS", "phase": "open", "day": "20260422", "blockers": []},
        },
    )

    payload = update_rollout_orchestrator_state(
        repo_root=tmp_path,
        rollout_state_path=tmp_path / "runtime/state/trading_rollout_state.json",
        output_path=tmp_path / "runtime/state/trading_rollout_orchestrator_state.json",
    )

    assert payload["opening_status"]["severity"] == "AUTO_FIXED"
    assert payload["opening_supervisor"]["open"]["actions"] == ["restart_run_system"]


def test_orchestrator_state_uses_same_day_opening_trace_when_standalone_is_stale(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "runtime/state/trading_rollout_state.json",
        {
            "last_day": "20260422",
            "last_recommendation": "CONTINUE_SHADOW",
            "paper_clean_days": 2,
            "min_paper_clean_days": 3,
            "qqq_shadow_observed_day_count": 2,
            "live_cutover_allowed": False,
        },
    )
    _write_json(tmp_path / "reports/daily_trading_evidence/20260422.json", {"day": "20260422", "recommendation": "CONTINUE_SHADOW", "blockers": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260421_preopen_smoke.json", {"day": "20260421", "blockers": ["no_entry_candidate_today"]})
    _write_json(
        tmp_path / "reports/qqq_kernel_shadow_parity/20260421_trace_coverage.json",
        {"day": "20260421", "missing_strategy_ids": ["qqq_intraday_001"]},
    )
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260421_postclose_report.json", {"day": "20260421", "blockers": ["no_entry_candidate_today"]})
    _write_json(
        tmp_path / "runtime/state/opening_day/20260422_open_supervisor.json",
        {
            "initial": {"recommendation": "PASS", "phase": "open", "day": "20260422", "blockers": []},
            "final": {
                "recommendation": "PASS",
                "phase": "open",
                "day": "20260422",
                "blockers": [],
                "trace_coverage": {"day": "20260422", "missing_strategy_ids": [], "recommendation": "PASS"},
            },
        },
    )

    payload = update_rollout_orchestrator_state(
        repo_root=tmp_path,
        rollout_state_path=tmp_path / "runtime/state/trading_rollout_state.json",
        output_path=tmp_path / "runtime/state/trading_rollout_orchestrator_state.json",
    )

    assert payload["blockers"] == []
    assert payload["current_priority_task"]["id"] == "paper_clean_days"
    assert payload["recent_reports"]["trace_coverage_source"] == "opening_supervisor"
    assert payload["recent_reports"]["preopen_smoke"] is None
    assert payload["recent_reports"]["postclose_report"] is None


def test_orchestrator_state_preserves_execution_tracking_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime/state/trading_rollout_state.json"
    output_path = tmp_path / "runtime/state/trading_rollout_orchestrator_state.json"
    _write_json(
        state_path,
        {
            "last_day": "20260422",
            "last_recommendation": "CONTINUE_SHADOW",
            "paper_clean_days": 1,
            "min_paper_clean_days": 3,
            "qqq_shadow_observed_day_count": 0,
            "live_cutover_allowed": False,
        },
    )
    _write_json(tmp_path / "reports/daily_trading_evidence/20260422.json", {"day": "20260422", "recommendation": "CONTINUE_SHADOW", "blockers": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260422_preopen_smoke.json", {"blockers": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260422_trace_coverage.json", {"missing_strategy_ids": []})
    _write_json(tmp_path / "reports/qqq_kernel_shadow_parity/20260422_postclose_report.json", {"blockers": []})
    _write_json(
        output_path,
        {
            "last_execution": {"task_id": "opening_fix", "status": "success"},
            "execution_history": [{"task_id": "opening_fix", "status": "success"}],
            "task_attempts": {"opening_fix": {"attempt_count": 1}},
            "last_completed_task": {"task_id": "opening_fix"},
        },
    )

    payload = update_rollout_orchestrator_state(
        repo_root=tmp_path,
        rollout_state_path=state_path,
        output_path=output_path,
    )

    assert payload["last_execution"]["task_id"] == "opening_fix"
    assert payload["task_attempts"]["opening_fix"]["attempt_count"] == 1
