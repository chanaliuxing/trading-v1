from tool.report.build_option_platform_coverage_report import build_option_platform_coverage_report


def test_build_option_platform_coverage_report_reaches_three_archetypes() -> None:
    report = build_option_platform_coverage_report("./config.yml")

    assert report["summary"]["strategy_sample_count"] >= 3
    assert report["summary"]["distinct_archetype_count"] >= 3
    assert report["summary"]["coverage_goal_met"] is True
    definition_ids = {row["definition_id"] for row in report["strategy_samples"]}
    assert "QQQ_INTRADAY_OPTION_PARITY_V1" in definition_ids
    assert "SPY_MA_CROSS_STACK_CONT_V2_DUAL" in definition_ids
    assert "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1" in definition_ids
