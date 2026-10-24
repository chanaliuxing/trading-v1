from datetime import datetime

from strategies.options_kernel.modules import definition_registry
from strategies.options_kernel.modules.direction_mapping import (
    DirectionMappingContext,
    StockSignalOptionDirectionMappingPolicy,
)
from strategies.options_kernel.types import KernelBarContext, KernelPositionState, PluginRuntimeState, Signal


def test_stock_signal_direction_mapping_policy_maps_long_short_to_call_put() -> None:
    policy = StockSignalOptionDirectionMappingPolicy()
    now = datetime(2026, 4, 2, 10, 0, 0)
    assert policy.map_signal_side(DirectionMappingContext(signal=Signal(ts=now, side="LONG", signal_kind="x"))) == "CALL"
    assert policy.map_signal_side(DirectionMappingContext(signal=Signal(ts=now, side="SHORT", signal_kind="x"))) == "PUT"


def test_qqq_intraday_definition_publishes_first_class_policy_ids() -> None:
    definition = definition_registry.get("QQQ_INTRADAY_OPTION_PARITY_V1")
    identity = definition.module_identity()

    assert identity["strategy_archetype"] == "stock_signal_option"
    assert identity["direction_mapping_policy"] == "stock_signal_to_option_side"
    assert identity["risk_policy_id"] == "qqq_intraday_underlying_stop"
    assert identity["profit_management_policy_id"] == "qqq_intraday_profit_management"
    assert identity["flatten_policy_id"] == "qqq_intraday_eod_flatten"


def test_qqq_intraday_position_policy_uses_composed_policies_for_tp1() -> None:
    definition = definition_registry.get("QQQ_INTRADAY_OPTION_PARITY_V1")
    pos = KernelPositionState(
        strategy_id="qqq_intraday_option_v1_001",
        position_lot_id="lot-tp1",
        opt_symbol="OPT:QQQ:20260402:500:C",
        side="CALL",
        qty=2.0,
        remaining_qty=2.0,
        opened_at=datetime(2026, 4, 2, 10, 0, 0),
        structure_stop=498.5,
        target_1r=500.75,
        target_1p2r=501.5,
        status="open",
        payload={"initial_qty": 2.0, "entry_atr_used": 1.0, "entry_underlying": 500.0},
    )
    runtime = PluginRuntimeState(
        strategy_id="qqq_intraday_option_v1_001",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=datetime(2026, 4, 2, 10, 5, 0),
        wall_now=datetime(2026, 4, 2, 10, 5, 0),
        spot=500.8,
        bar=KernelBarContext(
            ts=datetime(2026, 4, 2, 10, 5, 0),
            symbol="QQQ",
            open=500.2,
            high=500.9,
            low=500.1,
            close=500.8,
            volume=1000.0,
            vwap=500.3,
            atr=1.0,
            payload={"v3m": 0.3},
        ),
        features={"v3m": 0.3, "atr_eff": 1.0},
        active_position=pos,
        active_positions=[pos],
        payload={},
    )
    plan = definition.position_policy.build_exit_plan(type("Ctx", (), {"runtime": runtime, "extras": {}})())
    assert plan is not None
    assert plan.reason == "UNDERLYING_TP1_1R"


def test_ma_runner_position_policy_builds_exit_plans_for_all_active_positions() -> None:
    definition = definition_registry.get("QQQ_MA_CROSS_STACK_CONT_V2_DUAL")
    now = datetime(2026, 4, 2, 15, 51, 0)
    pos_a = KernelPositionState(
        strategy_id="qqq_ma_v2",
        position_lot_id="lot-a",
        opt_symbol="OPT:QQQ:20260403:500:C",
        side="CALL",
        qty=2.0,
        remaining_qty=2.0,
        opened_at=datetime(2026, 4, 2, 14, 10, 0),
        status="live",
    )
    pos_b = KernelPositionState(
        strategy_id="qqq_ma_v2",
        position_lot_id="lot-b",
        opt_symbol="OPT:QQQ:20260403:502:C",
        side="CALL",
        qty=1.0,
        remaining_qty=1.0,
        opened_at=datetime(2026, 4, 2, 14, 30, 0),
        status="live",
    )
    runtime = PluginRuntimeState(
        strategy_id="qqq_ma_v2",
        kernel_mode="pure_plugin",
        symbol="QQQ",
        now=now,
        wall_now=now,
        spot=500.0,
        bar=KernelBarContext(ts=now, symbol="QQQ", open=499.8, high=500.2, low=499.6, close=500.0, volume=1000.0, vwap=500.0, atr=0.8),
        active_position=pos_a,
        active_positions=[pos_a, pos_b],
        payload={"bar_history": [{"close": 500.0, "high": 500.2, "low": 499.6}]},
    )
    plans = definition.position_policy.build_exit_plans(type("Ctx", (), {"runtime": runtime, "extras": {"force_flatten_time": "15:50:00"}})())
    assert len(plans) == 2
    assert {plan.position_lot_id for plan in plans} == {"lot-a", "lot-b"}
    assert {plan.reason for plan in plans} == {"EOD_FORCE_FLATTEN"}
