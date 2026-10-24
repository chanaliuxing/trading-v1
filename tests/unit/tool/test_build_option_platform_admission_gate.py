from tool.report.build_option_platform_admission_gate import build_option_platform_admission_gate


def test_build_option_platform_admission_gate_current_platform_is_reviewable() -> None:
    report = build_option_platform_admission_gate("./config.yml")

    assert report["summary"]["active_option_strategy_count"] >= 2
    assert report["summary"]["distinct_archetype_count"] >= 3
    assert report["summary"]["coverage_goal_met"] is True
    assert report["summary"]["active_validation_fail_count"] == 0
    assert report["summary"]["legacy_active_count"] == 0
    assert report["recommendation"] == "PLATFORM_ADMISSION_REVIEW_ALLOWED"
    assert report["platform_admission_blocked"] is False
