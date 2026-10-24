from datetime import datetime

from strategies.options_kernel.modules.qqq_intraday_option_position_policy import QQQIntradayOptionPositionPolicy
from strategies.options_kernel.modules.qqq_intraday_stock_signal import QQQIntradayStockSignalModule
from strategies.options_kernel.types import KernelBarContext, KernelPositionState, PluginRuntimeState


def _runtime(*, now: datetime, spot: float, features: dict | None = None, pos: KernelPositionState | None = None) -> PluginRuntimeState:
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
            high=spot + 0.5,
            low=spot - 0.5,
            close=spot,
            volume=1000.0,
            vwap=float((features or {}).get("vwap", spot)),
            atr=float((features or {}).get("atr_eff", 1.0)),
            payload=dict(features or {}),
        ),
        features=dict(features or {}),
        active_position=pos,
        active_positions=[pos] if pos is not None else [],
        payload={},
    )


def test_signal_module_maps_positive_v3m_to_call() -> None:
    module = QQQIntradayStockSignalModule()
    runtime = _runtime(
        now=datetime(2026, 4, 2, 10, 0, 0),
        spot=500.0,
        features={"v3m": 0.65, "atr_eff": 1.0, "atr_seed": 1.0, "vwap": 499.5},
    )
    signal = module.build_signal(type("Ctx", (), {"runtime": runtime, "extras": {"entry_model": "baseline", "qqew_decoupled": True}})())
    assert signal is not None
    assert signal.side == "CALL"


def test_signal_module_blocks_outside_entry_window() -> None:
    module = QQQIntradayStockSignalModule()
    runtime = _runtime(
        now=datetime(2026, 4, 2, 15, 20, 0),
        spot=500.0,
        features={"v3m": 0.25, "atr_eff": 1.0, "vwap": 499.5},
    )
    signal = module.build_signal(
        type(
            "Ctx",
            (),
            {
                "runtime": runtime,
                "extras": {
                    "entry_start_am": "09:40:00",
                    "entry_end_am": "12:00:00",
                    "entry_start_pm": "12:00:00",
                    "entry_end_pm": "15:00:00",
                    "no_new_entry_after": "15:00:00",
                },
            },
        )()
    )
    assert signal is None


def test_position_policy_exits_on_underlying_hard_stop() -> None:
    pos = KernelPositionState(
        strategy_id="qqq_intraday_option_v1_001",
        position_lot_id="lot1",
        opt_symbol="OPT:QQQ:20260402:500:C",
        side="CALL",
        qty=2.0,
        remaining_qty=2.0,
        opened_at=datetime(2026, 4, 2, 10, 0, 0),
        structure_stop=499.25,
        target_1r=500.75,
        target_1p2r=501.20,
        status="open",
        payload={"initial_qty": 2.0, "entry_atr_used": 1.0},
    )
    runtime = PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=datetime(2026, 4, 2, 10, 5, 0),
        wall_now=datetime(2026, 4, 2, 10, 5, 0),
        spot=499.4,
        bar=KernelBarContext(
            ts=datetime(2026, 4, 2, 10, 5, 0),
            symbol="QQQ",
            open=500.0,
            high=500.1,
            low=499.2,
            close=499.4,
            volume=1000.0,
            vwap=499.7,
            atr=1.0,
            payload={"v3m": -0.3},
        ),
        features={"v3m": -0.3, "atr_eff": 1.0},
        active_position=pos,
        active_positions=[pos],
        payload={},
    )
    policy = QQQIntradayOptionPositionPolicy()
    plan = policy.build_exit_plan(type("Ctx", (), {"runtime": runtime, "extras": {}})())
    assert plan is not None
    assert plan.reason == "UNDERLYING_HARD_STOP"


def test_position_policy_exits_on_eod_flatten() -> None:
    pos = KernelPositionState(
        strategy_id="qqq_intraday_option_v1_001",
        position_lot_id="lot2",
        opt_symbol="OPT:QQQ:20260402:500:P",
        side="PUT",
        qty=1.0,
        remaining_qty=1.0,
        opened_at=datetime(2026, 4, 2, 14, 30, 0),
        structure_stop=501.0,
        target_1r=499.0,
        target_1p2r=498.5,
        status="open",
        payload={"initial_qty": 1.0, "entry_atr_used": 1.0},
    )
    runtime = _runtime(
        now=datetime(2026, 4, 2, 15, 51, 0),
        spot=499.3,
        features={"v3m": -0.2, "atr_eff": 1.0, "vwap": 499.8},
        pos=pos,
    )
    policy = QQQIntradayOptionPositionPolicy()
    plan = policy.build_exit_plan(type("Ctx", (), {"runtime": runtime, "extras": {"force_flatten_time": "15:50:00"}})())
    assert plan is not None
    assert plan.reason == "EOD_FORCE_FLATTEN"


def test_position_policy_exits_on_underlying_data_stale_guard() -> None:
    pos = KernelPositionState(
        strategy_id="qqq_intraday_option_v1_001",
        position_lot_id="lot3",
        opt_symbol="OPT:QQQ:20260402:500:C",
        side="CALL",
        qty=2.0,
        remaining_qty=2.0,
        opened_at=datetime(2026, 4, 2, 9, 55, 0),
        structure_stop=498.5,
        target_1r=501.0,
        target_1p2r=501.8,
        status="open",
        payload={"initial_qty": 2.0, "entry_atr_used": 1.0, "entry_underlying": 500.0},
    )
    runtime = _runtime(
        now=datetime(2026, 4, 2, 10, 5, 0),
        spot=500.2,
        features={"v3m": 0.1, "atr_eff": 1.0, "vwap": 500.0, "data_age_qqq_sec": 20.0},
        pos=pos,
    )
    policy = QQQIntradayOptionPositionPolicy()
    plan = policy.build_exit_plan(
        type("Ctx", (), {"runtime": runtime, "extras": {"logic_retreat_min_hold_sec": 60, "logic_retreat_max_primary_age_sec": 8.0}})()
    )
    assert plan is not None
    assert plan.reason == "UNDERLYING_DATA_STALE_GUARD"


def test_position_policy_exits_on_breakeven_shield() -> None:
    pos = KernelPositionState(
        strategy_id="qqq_intraday_option_v1_001",
        position_lot_id="lot4",
        opt_symbol="OPT:QQQ:20260402:500:C",
        side="CALL",
        qty=2.0,
        remaining_qty=2.0,
        opened_at=datetime(2026, 4, 2, 10, 0, 0),
        structure_stop=498.5,
        target_1r=501.0,
        target_1p2r=501.8,
        status="open",
        payload={"initial_qty": 2.0, "entry_atr_used": 1.0, "entry_underlying": 500.0},
    )
    runtime = PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=datetime(2026, 4, 2, 10, 20, 0),
        wall_now=datetime(2026, 4, 2, 10, 20, 0),
        spot=501.1,
        bar=KernelBarContext(
            ts=datetime(2026, 4, 2, 10, 20, 0),
            symbol="QQQ",
            open=500.3,
            high=501.3,
            low=500.1,
            close=501.1,
            volume=1200.0,
            vwap=500.4,
            atr=1.0,
            payload={"v3m": 0.2},
        ),
        features={"v3m": 0.2, "atr_eff": 1.0, "vwap": 500.4},
        active_position=pos,
        active_positions=[pos],
        payload={},
    )
    policy = QQQIntradayOptionPositionPolicy()
    plan = policy.build_exit_plan(type("Ctx", (), {"runtime": runtime, "extras": {"enable_breakeven_shield": True}})())
    assert plan is not None
    assert plan.reason == "UNDERLYING_BREAKEVEN_SHIELD"
