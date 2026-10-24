from tool.report.build_option_strategy_rollout_evidence_report import build_option_strategy_rollout_evidence_report


def test_build_option_strategy_rollout_evidence_report_passes_active_contracts() -> None:
    report = build_option_strategy_rollout_evidence_report("./config.yml")

    assert report["summary"]["active_strategy_count"] >= 2
    assert report["summary"]["active_rollout_contract_fail_count"] == 0
    assert "qqq_intraday_option_v1_001" in {row["strategy_id"] for row in report["strategies"]}
    qqq = next(row for row in report["strategies"] if row["strategy_id"] == "qqq_intraday_option_v1_001")
    assert qqq["evidence_contract_id"] == "qqq_intraday_option_rollout_v1"
    assert qqq["state_shadow_required"] is True
