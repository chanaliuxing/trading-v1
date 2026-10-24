from datetime import datetime

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin


def test_replay_signal_does_not_use_option_chain_as_stock_truth() -> None:
    now = datetime(2026, 4, 9, 10, 0, 0)
    runtime = PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=0.0,
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=0.0,
            high=0.0,
            low=0.0,
            close=0.0,
            volume=0.0,
            vwap=0.0,
            atr=0.0,
            payload={
                "option_chain_mid": 1.23,
                "option_chain_iv": 0.25,
                "option_chain_spread_pct": 0.05,
            },
        ),
        features={
            "option_chain_mid": 1.23,
            "option_chain_iv": 0.25,
            "option_chain_spread_pct": 0.05,
        },
        payload={},
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras({"signal_source_mode": "independent_rule_replay"})

    signal = plugin.build_signal_pure(runtime)
    assert signal is None
