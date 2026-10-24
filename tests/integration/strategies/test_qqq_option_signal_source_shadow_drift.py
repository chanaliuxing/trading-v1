from datetime import datetime

from strategies.options_kernel.types import KernelBarContext, PluginRuntimeState
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin
from strategies.qqq_stock_signal_artifact import clear_stock_entry_signal_artifacts, publish_stock_entry_signal


def _runtime(now: datetime, *, v3m: float, spot: float = 500.1, vwap: float = 500.0) -> PluginRuntimeState:
    features = {
        "v3m": v3m,
        "vwap": vwap,
        "atr_live": 1.0,
        "atr_seed": 1.0,
        "atr_eff": 1.0,
        "vol_surge_ratio": 1.2,
        "vol_t": 1500.0,
        "vol_ma10": 1200.0,
        "distance_from_vwap_atr": 0.15,
        "data_age_qqq_sec": 0.0,
        "data_age_qqew_sec": 0.0,
        "continuity_180s_ok": True,
        "qqew_now": 99.0,
        "qqew_vwap_now": 98.9,
        "qqew_decoupled": True,
        "close_by_minute": {now.replace(second=0, microsecond=0): spot},
    }
    return PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=spot,
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=spot,
            high=spot + 0.2,
            low=spot - 0.2,
            close=spot,
            volume=1500.0,
            vwap=vwap,
            atr=1.0,
            payload=features,
        ),
        features=features,
        payload={},
    )


def test_shadow_drift_records_mismatch_without_changing_live_authority() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 30, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )

    live_plugin = QQQIntradayOptionPlugin()
    live_plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )
    shadow_plugin = QQQIntradayOptionPlugin()
    shadow_plugin.apply_default_extras(
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

    live = live_plugin.build_signal_pure(_runtime(now, v3m=-0.9, spot=500.1, vwap=500.0))
    shadow = shadow_plugin.build_signal_pure(_runtime(now, v3m=-0.9, spot=499.4, vwap=500.0))

    assert live is not None
    assert live.side == "CALL"  # authority from upstream stock signal
    assert shadow is not None
    assert shadow.side == "PUT"  # replay sees local feature direction

    parity = "parity_ok" if live.side == shadow.side else "drift_detected"
    assert parity == "drift_detected"
