from tool.report.build_option_assembly_adapter_report import build_option_assembly_adapter_report


def test_build_option_assembly_adapter_report_passes_for_current_active_option_strategies() -> None:
    report = build_option_assembly_adapter_report("./config.yml")

    assert report["summary"]["active_option_strategy_count"] >= 2
    assert report["summary"]["assembly_adapter_fail_count"] == 0
    ids = {row["strategy_id"] for row in report["strategies"]}
    assert "qqq_intraday_option_v1_001" in ids
    assert "spy_1dte_ma10_reject_0324_001" in ids
    for row in report["strategies"]:
        assert row["pass"] is True
        assert row["identity"]["definition_id"]
        assert row["identity"]["signal_module_id"]
        assert row["identity"]["flatten_policy_id"]
