from tool.build_stocks_kernel_instance_config import (
    build_admission_report,
    build_instance_config,
    build_plugin_instance_config,
)


def test_build_instance_config_defaults_to_smh_triplet():
    payload = build_instance_config(
        strategy_id="stocks_kernel_smh_001",
        strategy_template="trend_follow_equity",
        signal_symbol="SMH",
        long_exec_symbol="SOXL",
        short_exec_symbol="SOXS",
    )

    assert payload["id"] == "stocks_kernel_smh_001"
    assert payload["module"] == "strategies.stocks_kernel_strategy"
    assert payload["class"] == "StocksKernelStrategy"
    assert payload["params"]["extras"]["universe"][0]["signal_symbol"] == "SMH"
    assert payload["params"]["extras"]["universe"][0]["long_exec_symbol"] == "SOXL"
    assert payload["params"]["extras"]["universe"][0]["short_exec_symbol"] == "SOXS"
    assert payload["params"]["extras"]["dry_run"] is True


def test_build_instance_config_supports_live_overrides():
    payload = build_instance_config(
        strategy_id="stocks_kernel_breakout_001",
        strategy_template="breakout_equity",
        signal_symbol="NVDA",
        long_exec_symbol="NVDL",
        short_exec_symbol="NVDS",
        enabled=True,
        dry_run=False,
        policy_params={"hard_stop_atr_mult": 0.6},
        gate_overrides={"risk_budget": {"max_notional": 25000}},
    )

    assert payload["enabled"] is True
    assert payload["params"]["extras"]["dry_run"] is False
    assert payload["params"]["extras"]["policy_params"]["hard_stop_atr_mult"] == 0.6
    assert payload["params"]["extras"]["gate_overrides"]["risk_budget"]["max_notional"] == 25000


def test_build_instance_config_supports_confirm_symbol():
    payload = build_instance_config(
        strategy_id="stocks_kernel_spy_001",
        strategy_template="spy_intraday_core",
        signal_symbol="SPY",
        long_exec_symbol="SPXL",
        short_exec_symbol="SPXS",
        confirm_symbol="RSP",
    )

    assert payload["params"]["extras"]["universe"][0]["signal_symbol"] == "SPY"
    assert payload["params"]["extras"]["universe"][0]["confirm_symbol"] == "RSP"


def test_build_plugin_instance_config_emits_production_admission_go():
    payload = build_plugin_instance_config(
        plugin_cls="strategies.stocks_kernel.plugins.nvda_minute_flip:NVDAMinuteFlipStockPlugin",
        strategy_id="nvda_minute_flip_paper_001",
        enabled=True,
        dry_run=False,
        capital_limit_usd=250,
        qty=1,
        plugin_params={"order_type": "LMT", "limit_offset_bp": 20},
    )

    report = build_admission_report(payload)

    assert payload["params"]["extras"]["plugin_cls"] == (
        "strategies.stocks_kernel.plugins.nvda_minute_flip:NVDAMinuteFlipStockPlugin"
    )
    assert payload["params"]["extras"]["no_new_after_et"] == "23:30"
    assert report["go_no_go"] == "go"
    assert report["blockers"] == []
