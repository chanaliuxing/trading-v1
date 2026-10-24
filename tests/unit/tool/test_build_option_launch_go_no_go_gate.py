from tool.report.build_option_launch_go_no_go_gate import build_option_launch_go_no_go_gate


def test_build_option_launch_go_no_go_gate_blocks_when_active_paths_not_ready() -> None:
    report = build_option_launch_go_no_go_gate(
        platform_report={"platform_admission_blocked": False, "recommendation": "PLATFORM_ADMISSION_REVIEW_ALLOWED"},
        rollout_report={"recommendation": "ROLLOUT_CONTRACT_READY"},
        active_evidence_report={
            "recommendation": "BLOCK",
            "strategies": [
                {"strategy_id": "qqq_intraday_option_v1_001", "reasons": ["qqq_state_shadow_cutover_blocked"]},
                {
                    "strategy_id": "spy_1dte_ma10_reject_0324_001",
                    "reasons": ["spy_replay_parity_no_real_matched_day", "spy_live_shadow_no_matched_entries"],
                },
            ],
        },
        qqq_cutover_report={
            "recommendation": "BLOCK",
            "cutover_still_blocked": True,
            "post_instrumentation_day_count": 1,
            "post_instrumentation_pass_days": 0,
            "post_instrumentation_fail_days": 1,
        },
    )
    assert report["recommendation"] == "BLOCK"
    assert "qqq_cutover_blocked" in report["blockers"]
    assert "spy_replay_not_closed" in report["blockers"]
    assert "spy_live_shadow_not_closed" in report["blockers"]


def test_build_option_launch_go_no_go_gate_go_when_all_green() -> None:
    report = build_option_launch_go_no_go_gate(
        platform_report={"platform_admission_blocked": False, "recommendation": "PLATFORM_ADMISSION_REVIEW_ALLOWED"},
        rollout_report={"recommendation": "ROLLOUT_CONTRACT_READY"},
        active_evidence_report={
            "recommendation": "ACTIVE_STRATEGIES_EVIDENCE_READY",
            "strategies": [
                {"strategy_id": "qqq_intraday_option_v1_001", "reasons": []},
                {"strategy_id": "spy_1dte_ma10_reject_0324_001", "reasons": []},
            ],
        },
        qqq_cutover_report={
            "recommendation": "READY",
            "cutover_still_blocked": False,
            "post_instrumentation_day_count": 3,
            "post_instrumentation_pass_days": 3,
            "post_instrumentation_fail_days": 0,
        },
    )
    assert report["recommendation"] == "GO"
    assert report["blocker_count"] == 0
