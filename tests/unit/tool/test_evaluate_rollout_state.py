from __future__ import annotations

import json
from pathlib import Path

from tool.ops.evaluate_rollout_state import evaluate_rollout_state


def _write_evidence(path: Path, *, day: str, recommendation: str = "PASS", paper_clean: bool = True, live_candidate: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "day": day,
                "recommendation": recommendation,
                "blockers": [],
                "paper_clean_candidate": paper_clean,
                "live_cutover_candidate": live_candidate,
                "gates": {
                    "qqq_shadow_drift": {
                        "observed_days": ["20260419", "20260420", "20260421"],
                        "unresolved_diff_count": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_rollout_state_requires_three_clean_paper_days(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime/state/trading_rollout_state.json"
    for day in ["20260421", "20260422"]:
        evidence_path = tmp_path / f"reports/daily_trading_evidence/{day}.json"
        _write_evidence(evidence_path, day=day)
        state = evaluate_rollout_state(evidence_path=evidence_path, state_path=state_path, output_path=state_path)

    assert state["paper_clean_days"] == 2
    assert state["live_cutover_allowed"] is False

    evidence_path = tmp_path / "reports/daily_trading_evidence/20260423.json"
    _write_evidence(evidence_path, day="20260423")
    state = evaluate_rollout_state(evidence_path=evidence_path, state_path=state_path, output_path=state_path)

    assert state["paper_clean_days"] == 3
    assert state["live_cutover_allowed"] is True
    assert state["generated_at"]


def test_rollout_state_is_idempotent_for_same_day(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime/state/trading_rollout_state.json"
    evidence_path = tmp_path / "reports/daily_trading_evidence/20260421.json"
    _write_evidence(evidence_path, day="20260421")

    first = evaluate_rollout_state(evidence_path=evidence_path, state_path=state_path, output_path=state_path)
    second = evaluate_rollout_state(evidence_path=evidence_path, state_path=state_path, output_path=state_path)

    assert first["paper_clean_days"] == 1
    assert second["paper_clean_days"] == 1
    assert second["paper_clean_day_ids"] == ["20260421"]
    assert second["generated_at"]


def test_rollout_state_does_not_count_hold_as_clean_day(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime/state/trading_rollout_state.json"
    evidence_path = tmp_path / "reports/daily_trading_evidence/20260421.json"
    _write_evidence(evidence_path, day="20260421", recommendation="HOLD", paper_clean=False, live_candidate=False)

    state = evaluate_rollout_state(evidence_path=evidence_path, state_path=state_path, output_path=state_path)

    assert state["paper_clean_days"] == 0
    assert state["live_cutover_allowed"] is False


def test_rollout_state_removes_same_day_when_evidence_is_downgraded(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime/state/trading_rollout_state.json"
    evidence_path = tmp_path / "reports/daily_trading_evidence/20260421.json"

    _write_evidence(evidence_path, day="20260421", recommendation="PASS", paper_clean=True, live_candidate=True)
    first = evaluate_rollout_state(evidence_path=evidence_path, state_path=state_path, output_path=state_path)

    _write_evidence(evidence_path, day="20260421", recommendation="HOLD", paper_clean=False, live_candidate=False)
    second = evaluate_rollout_state(evidence_path=evidence_path, state_path=state_path, output_path=state_path)

    assert first["paper_clean_day_ids"] == ["20260421"]
    assert second["paper_clean_day_ids"] == []
    assert second["paper_clean_days"] == 0
    assert second["live_cutover_allowed"] is False
