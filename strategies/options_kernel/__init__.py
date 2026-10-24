from .base import OptionKernel, OptionKernelStrategy
from .execution_profile import build_execution_profile_from_extras
from .ownership import OptionOwnershipLedger
from .parity import OptionParityRunner, normalize_envelope
from .plugin import BaseOptionPlugin, OptionPlugin
from .test_harness import OptionPluginHarnessResult, OptionPluginTestHarness
from .types import (
    EntryIntent,
    EntryPlan,
    ExecutionProfile,
    ExitPlan,
    IBKRTrailingStopOrderSpec,
    KernelIntentRecord,
    OrderAck,
    OrderReject,
    ParityDiff,
    PositionLot,
    Selection,
    Signal,
    UnderlyingTriggeredOptionExitPlan,
)

__all__ = [
    "BaseOptionPlugin",
    "EntryIntent",
    "EntryPlan",
    "ExecutionProfile",
    "ExitPlan",
    "IBKRTrailingStopOrderSpec",
    "KernelIntentRecord",
    "OrderAck",
    "OrderReject",
    "OptionKernel",
    "OptionKernelStrategy",
    "OptionOwnershipLedger",
    "OptionPluginHarnessResult",
    "OptionPluginTestHarness",
    "OptionParityRunner",
    "OptionPlugin",
    "ParityDiff",
    "PositionLot",
    "Selection",
    "Signal",
    "UnderlyingTriggeredOptionExitPlan",
    "build_execution_profile_from_extras",
    "run_clickhouse_day_parity_scenario",
    "run_clickhouse_day_parity_scenario_sync",
    "run_direct_entry_parity_scenario",
    "run_direct_entry_parity_scenario_sync",
    "run_minute_session_parity_scenario",
    "run_minute_session_parity_scenario_sync",
    "normalize_envelope",
]


def __getattr__(name: str):
    if name in {
        "run_clickhouse_day_parity_scenario",
        "run_clickhouse_day_parity_scenario_sync",
        "run_direct_entry_parity_scenario",
        "run_direct_entry_parity_scenario_sync",
        "run_minute_session_parity_scenario",
        "run_minute_session_parity_scenario_sync",
    }:
        from . import harness as _harness

        return getattr(_harness, name)
    raise AttributeError(name)
