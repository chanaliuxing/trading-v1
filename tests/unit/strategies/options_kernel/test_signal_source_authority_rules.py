import pytest

from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin


def test_signal_source_mode_rejects_unknown_value() -> None:
    plugin = QQQIntradayOptionPlugin()
    with pytest.raises(ValueError, match="invalid signal_source_mode"):
        plugin.apply_default_extras({"signal_source_mode": "unknown_mode"})


def test_signal_source_fallback_mode_rejects_live_replay_fallback() -> None:
    plugin = QQQIntradayOptionPlugin()
    with pytest.raises(ValueError, match="invalid signal_source_fallback_mode"):
        plugin.apply_default_extras(
            {
                "signal_source_mode": "upstream_stock_signal",
                "signal_source_fallback_mode": "independent_rule_replay",
            }
        )


def test_upstream_mode_empty_source_strategy_id_is_backfilled() -> None:
    plugin = QQQIntradayOptionPlugin()
    cfg = plugin._signal_source_config(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "",
        }
    )
    assert cfg["source_strategy_id"] == "qqq_intraday_plugin_001"
