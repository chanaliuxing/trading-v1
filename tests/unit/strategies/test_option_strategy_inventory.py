from pathlib import Path

import yaml

from strategies.options_kernel.inventory import (
    DEFAULT_RETIRED_OPTION_STRATEGY_IDS,
    build_option_strategy_inventory,
    build_option_strategy_inventory_from_config,
)


def test_build_option_strategy_inventory_classifies_active_disabled_and_retired() -> None:
    payload = {
        "strategies": [
            {
                "id": "qqq_intraday_option_v1_001",
                "enabled": True,
                "module": "strategies.qqq_intraday_option_v1_strategy",
                "class": "QQQIntradayOptionV1Strategy",
                "params": {"extras": {"underlying": "QQQ", "risk_symbol": "OPT:QQQ:0DTE", "kernel_mode": "pure_plugin"}},
            },
            {
                "id": "qqq_1dte_ma10_reject_0324_v2_001",
                "enabled": False,
                "module": "strategies.qqq_1dte_ma10_reject_v2_strategy",
                "class": "QQQ1DTEMA10RejectV2Strategy",
                "params": {"extras": {"underlying": "QQQ", "risk_symbol": "OPT:QQQ:1DTE", "kernel_mode": "pure_plugin"}},
            },
            {
                "id": "bull_put_spread_qqq",
                "enabled": False,
                "module": "strategies.options.bull_put_spread_strategy",
                "class": "BullPutSpreadStrategy",
                "params": {"extras": {}},
            },
        ]
    }

    report = build_option_strategy_inventory(payload)

    assert report["active_strategy_ids"] == ["qqq_intraday_option_v1_001"]
    assert report["retired_strategy_ids"] == ["qqq_1dte_ma10_reject_0324_v2_001"]
    assert report["disabled_strategy_ids"] == ["bull_put_spread_qqq"]
    assert report["summary"]["option_strategy_count"] == 3


def test_build_option_strategy_inventory_from_real_config_has_expected_active_and_retired_ids() -> None:
    report = build_option_strategy_inventory_from_config(Path("./config.yml"))

    assert "qqq_intraday_option_v1_001" in report["active_strategy_ids"]
    assert "spy_1dte_ma10_reject_0324_001" in report["active_strategy_ids"]
    for strategy_id in DEFAULT_RETIRED_OPTION_STRATEGY_IDS:
        assert strategy_id in report["retired_strategy_ids"]


def test_inventory_builder_reads_temp_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "strategies": [
                    {
                        "id": "test_option_001",
                        "enabled": True,
                        "module": "strategies.test_option_strategy",
                        "class": "TestOptionStrategy",
                        "params": {"extras": {"risk_symbol": "OPT:QQQ:0DTE"}},
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = build_option_strategy_inventory_from_config(config_path, retired_ids=())
    assert report["active_strategy_ids"] == ["test_option_001"]
