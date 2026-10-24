from datetime import datetime

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin


def _runtime(now: datetime) -> PluginRuntimeState:
    features = {
        "v3m": 0.9,
        "vwap": 499.8,
        "atr_live": 1.0,
        "atr_seed": 1.0,
        "atr_eff": 1.0,
        "vol_surge_ratio": 1.3,
        "vol_t": 1800.0,
        "vol_ma10": 1500.0,
        "distance_from_vwap_atr": 0.2,
        "data_age_qqq_sec": 0.0,
        "data_age_qqew_sec": 0.0,
        "continuity_180s_ok": True,
        "qqew_now": 99.0,
        "qqew_vwap_now": 98.9,
        "qqew_decoupled": True,
        "close_by_minute": {
            now.replace(second=0, microsecond=0): 500.2,
        },
    }
    return PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=500.2,
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=500.0,
            high=500.4,
            low=499.7,
            close=500.2,
            volume=1800.0,
            vwap=499.8,
            atr=1.0,
            payload=features,
        ),
        features=features,
        payload={},
    )


def test_independent_rule_replay_source_builds_signal() -> None:
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "independent_rule_replay",
            "entry_model": "baseline",
            "qqew_decoupled": True,
            "entry_recipe": {
                "session_module": "baseline_rth_window",
                "direction_module": "baseline_direction",
                "guards": [],
                "overlays": [],
                "translator": "qqq_option_translation",
            },
        }
    )
    signal = plugin.build_signal_pure(_runtime(datetime(2026, 4, 8, 10, 15, 0)))
    assert signal is not None
    assert signal.side in {"CALL", "PUT"}
    assert signal.payload.get("signal_source") == "qqq_intraday_rule_engine"
