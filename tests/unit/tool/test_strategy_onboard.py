from tool.strategy_onboard import build_option_onboard_plan
from services.production.strategy_data_contract import validate_strategy_production_manifests


def test_build_option_onboard_plan_contains_auditable_config_and_baseline_diff():
    plan = build_option_onboard_plan(
        strategy_id="vegas_hma_setup_trigger_risk_v12_spy_001",
        plugin="vegas_hma_setup_trigger_risk_v12_plugin",
        underlying="SPY",
    )

    assert plan["artifact_type"] == "strategy_onboard_plan"
    assert plan["config_patch"]["strategies[]"]["id"] == "vegas_hma_setup_trigger_risk_v12_spy_001"
    assert plan["config_patch"]["strategies[]"]["params"]["extras"]["kernel_mode"] == "pure_plugin"
    assert plan["phase0_baseline_diff"]["action"] == "add_strategy_to_baseline"
    assert plan["docs_stub"]["path"].endswith("vegas_hma_setup_trigger_risk_v12_spy_001.md")
    strategy = plan["config_patch"]["strategies[]"]
    strategy["enabled"] = True
    validation = validate_strategy_production_manifests({"strategies": [strategy]}, mandatory_strategy_ids=[strategy["id"]])
    assert validation.blockers == ()
