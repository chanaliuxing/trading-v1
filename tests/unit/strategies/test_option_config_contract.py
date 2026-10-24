from strategies.options_kernel.config_contract import validate_option_strategy_config_row


def _row() -> dict:
    return {
        "id": "spy_intraday_momentum_option_paper_001",
        "enabled": False,
        "module": "strategies.spy_intraday_momentum_option_paper_strategy",
        "class": "SPYIntradayMomentumOptionPaperStrategy",
        "params": {
            "extras": {
                "definition_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1",
                "underlying": "SPY",
                "risk_symbol": "OPT:SPY:0DTE",
                "shared_cap_group_id": "SPY_INTRADAY_MOMENTUM_OPTION_PAPER",
                "shared_cap_group_strategy_ids": ["spy_intraday_momentum_option_paper_001"],
                "kernel_mode": "pure_plugin",
                "strict_scope_validation": True,
            }
        },
    }


def test_validate_option_strategy_config_row_passes_for_definition_driven_plugin() -> None:
    result = validate_option_strategy_config_row(_row())

    assert result["pass"] is True
    assert result["resolved_identity"]["definition_id"] == "SPY_INTRADAY_MOMENTUM_OPTION_PAPER_V1"
    assert result["resolved_identity"]["strategy_archetype"] == "generic_intraday_momentum"


def test_validate_option_strategy_config_row_detects_missing_self_in_shared_cap_group() -> None:
    row = _row()
    row["params"]["extras"]["shared_cap_group_strategy_ids"] = ["other_strategy_001"]

    result = validate_option_strategy_config_row(row)

    assert result["pass"] is False
    assert "shared_cap_group_strategy_ids_missing_self" in result["issues"]
