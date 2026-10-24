import json
from pathlib import Path

from tool.report.build_option_active_strategy_evidence_status import build_option_active_strategy_evidence_status


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_build_option_active_strategy_evidence_status_blocked_by_real_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    platform = {"platform_admission_blocked": False, "recommendation": "PLATFORM_ADMISSION_REVIEW_ALLOWED"}
    rollout = {
        "recommendation": "ROLLOUT_CONTRACT_READY",
        "strategies": [
            {"strategy_id": "qqq_intraday_option_v1_001", "status": "active", "rollout_contract_pass": True, "issues": []},
            {"strategy_id": "spy_1dte_ma10_reject_0324_001", "status": "active", "rollout_contract_pass": True, "issues": []},
        ],
    }

    _write_json(
        state_root / "qqq_state_shadow_acceptance" / "qqq_state_shadow_cutover_recommendation.json",
        {"recommendation": "BLOCK", "cutover_still_blocked": True, "reasons": ["insufficient_pass_days"]},
    )
    _write_json(
        state_root / "spy_option_ma_parity_2026-03-30.json",
        {
            "summary": {"days_total": 1, "matched_day_count": 0},
            "days": [{"day": "2026-03-30", "matched": False, "old_count": 3, "new_count": 7}],
        },
    )
    _write_json(
        state_root / "spy_option_ma_shadow_parity_2026-03-30.json",
        {"matched_entries": 0, "replay_expected_entries": 3, "live_shadow_entries": 0, "trade_parity": 0.0},
    )

    report = build_option_active_strategy_evidence_status(platform, rollout, state_root=state_root)
    assert report["recommendation"] == "BLOCK"
    assert report["blocked_strategy_count"] == 2
    by_id = {row["strategy_id"]: row for row in report["strategies"]}
    assert "qqq_state_shadow_cutover_blocked" in by_id["qqq_intraday_option_v1_001"]["reasons"]
    assert "spy_replay_parity_no_real_matched_day" in by_id["spy_1dte_ma10_reject_0324_001"]["reasons"]


def test_build_option_active_strategy_evidence_status_ready_when_all_green(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    platform = {"platform_admission_blocked": False, "recommendation": "PLATFORM_ADMISSION_REVIEW_ALLOWED"}
    rollout = {
        "recommendation": "ROLLOUT_CONTRACT_READY",
        "strategies": [
            {"strategy_id": "qqq_intraday_option_v1_001", "status": "active", "rollout_contract_pass": True, "issues": []},
            {"strategy_id": "spy_1dte_ma10_reject_0324_001", "status": "active", "rollout_contract_pass": True, "issues": []},
        ],
    }
    _write_json(
        state_root / "qqq_state_shadow_acceptance" / "qqq_state_shadow_cutover_recommendation.json",
        {"recommendation": "READY", "cutover_still_blocked": False, "post_instrumentation_pass_days": 3},
    )
    _write_json(
        state_root / "spy_option_ma_parity_2026-03-30.json",
        {
            "summary": {"days_total": 1, "matched_day_count": 1},
            "days": [{"day": "2026-03-30", "matched": True, "old_count": 3, "new_count": 3}],
        },
    )
    _write_json(
        state_root / "spy_option_ma_shadow_parity_2026-03-30.json",
        {"matched_entries": 3, "replay_expected_entries": 3, "live_shadow_entries": 3, "trade_parity": 1.0},
    )

    report = build_option_active_strategy_evidence_status(platform, rollout, state_root=state_root)
    assert report["recommendation"] == "ACTIVE_STRATEGIES_EVIDENCE_READY"
    assert report["blocked_strategy_count"] == 0
