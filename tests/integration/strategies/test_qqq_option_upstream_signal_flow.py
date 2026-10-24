from datetime import datetime, timedelta

from strategies.options_kernel.types import KernelBarContext, KernelPositionState, PluginRuntimeState
from strategies.options_plugins.qqq_intraday_option import QQQIntradayOptionPlugin
from strategies.qqq_stock_signal_artifact import clear_stock_entry_signal_artifacts, publish_stock_entry_signal


def _runtime(now: datetime, *, pending: int = 0, with_position: bool = False) -> PluginRuntimeState:
    features = {
        "v3m": 0.7,
        "vwap": 500.0,
        "atr_live": 1.0,
        "atr_seed": 1.0,
        "atr_eff": 1.0,
        "vol_surge_ratio": 1.2,
        "distance_from_vwap_atr": 0.15,
        "qqew_now": 99.0,
        "qqew_vwap": 98.9,
        "qqew_decoupled": True,
        "close_by_minute": {now.replace(second=0, microsecond=0): 500.1},
    }
    pos = None
    poses = []
    if with_position:
        pos = KernelPositionState(
            strategy_id="qqq_intraday_option_v1_001",
            position_lot_id="lot1",
            opt_symbol="OPT:QQQ:20260408:500:C",
            side="CALL",
            qty=1.0,
            remaining_qty=1.0,
            opened_at=now,
        )
        poses = [pos]
    return PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=500.1,
        bar=KernelBarContext(
            ts=now,
            symbol="QQQ",
            open=500.0,
            high=500.3,
            low=499.8,
            close=500.1,
            volume=1500.0,
            vwap=500.0,
            atr=1.0,
            payload=features,
        ),
        features=features,
        active_position=pos,
        active_positions=poses,
        payload={"same_strategy_pending_orders": pending},
    )


def test_upstream_signal_flow_long_to_call_and_stale_guard() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 20, 0)
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
            "signal_source_max_signal_age_sec": 5.0,
        }
    )

    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    live_signal = plugin.build_signal_pure(_runtime(now))
    assert live_signal is not None
    assert live_signal.side == "CALL"

    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now - timedelta(seconds=20),
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    stale_signal = plugin.build_signal_pure(_runtime(now))
    assert stale_signal is None


def test_upstream_signal_flow_suppresses_when_pending_or_positioned() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 21, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    plugin = QQQIntradayOptionPlugin()
    plugin.apply_default_extras(
        {
            "signal_source_mode": "upstream_stock_signal",
            "signal_source_source_strategy_id": "qqq_intraday_001",
        }
    )

    assert plugin.build_signal_pure(_runtime(now, pending=1)) is None
    assert plugin.build_signal_pure(_runtime(now, with_position=True)) is None
