from datetime import datetime

from libs.strategy.kernel_contracts import (
    ExecutionProfile,
    IBKRTrailingStopOrderSpec,
    KernelIntentRecord,
    UnderlyingTriggeredOptionExitPlan,
)
from strategies.options_kernel.types import ExecutionProfile as OptionExecutionProfile
from strategies.options_kernel.types import KernelIntentRecord as OptionKernelIntentRecord
from strategies.options_kernel.types import UnderlyingTriggeredOptionExitPlan as OptionUnderlyingExitPlan


def test_options_kernel_types_reexport_shared_contracts():
    profile = OptionExecutionProfile(
        profile_id="p1",
        market_data_profile_id="md1",
        timeout_bars=3,
    )
    intent = OptionKernelIntentRecord(
        strategy_id="s1",
        decision_id="d1",
        symbol="QQQ",
        target_qty=1.0,
        ts_bar_end_us=123,
    )

    assert isinstance(profile, ExecutionProfile)
    assert isinstance(intent, KernelIntentRecord)


def test_underlying_triggered_option_exit_plan_is_shared_platform_contract():
    plan = OptionUnderlyingExitPlan(
        underlying_symbol="SPY",
        trigger_price=510.25,
        trigger_direction="BELOW",
        option_contract_id="OPT:SPY:20260515:510:P",
        exit_action="SELL",
        quantity=2,
        ibkr_trailing_order_spec=IBKRTrailingStopOrderSpec(
            action="SELL",
            orderType="TRAIL",
            totalQuantity=2,
            trailingPercent=15.0,
        ),
    )
    exit_plan = plan.to_exit_plan(
        ts=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
        position_lot_id="lot-1",
    )

    assert isinstance(plan, UnderlyingTriggeredOptionExitPlan)
    assert exit_plan.position_lot_id == "lot-1"
    assert exit_plan.payload["exit_model"] == "UNDERLYING_TRIGGERED_OPTION_EXIT"
    assert exit_plan.payload["underlying_trigger"]["trigger_direction"] == "BELOW"
