from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from libs.strategy.base import StrategyParams

from .plugin import BaseOptionPlugin
from .types import EntryPlan, ExecutionProfile, KernelBarContext, OrderReject, PluginRuntimeState, Selection, Signal


ET = ZoneInfo("America/New_York")


@dataclass
class OptionPluginHarnessResult:
    signal: Optional[Signal] = None
    entry_plan: Optional[EntryPlan] = None
    selection: Optional[Selection] = None
    execution_profile: Optional[ExecutionProfile] = None
    rejected: bool = False
    reject_reason: str = ""
    lifecycle_events: list[dict[str, Any]] = field(default_factory=list)


class OptionPluginTestHarness:
    """Small production-path harness for pure option plugins.

    It drives the standard pure-plugin path without IB, so strategy authors can
    test warmup, contract reject, execution-profile reject, and lifecycle rollback
    behavior without mocking the full runtime engine.
    """

    def __init__(
        self,
        plugin: BaseOptionPlugin,
        *,
        strategy_id: str = "option_plugin_harness_001",
        symbol: str = "QQQ",
        params: StrategyParams | None = None,
    ) -> None:
        self.plugin = plugin
        self.strategy_id = str(strategy_id)
        self.symbol = str(symbol).upper()
        self.params = params or StrategyParams()
        self.lifecycle_events: list[dict[str, Any]] = []

    def runtime(
        self,
        *,
        now: datetime | None = None,
        spot: float = 0.0,
        features: dict[str, Any] | None = None,
        history_bars: list[dict[str, Any]] | None = None,
        active_position: Any = None,
    ) -> PluginRuntimeState:
        ts = (now or datetime.now(ET)).astimezone(ET)
        bar = KernelBarContext(
            ts=ts,
            symbol=self.symbol,
            close=float(spot or 0.0),
            payload={"history_bars": list(history_bars or [])},
        )
        return PluginRuntimeState(
            strategy_id=self.strategy_id,
            kernel_mode="pure_plugin",
            symbol=self.symbol,
            now=ts,
            wall_now=ts,
            spot=float(spot or 0.0),
            bar=bar,
            features=dict(features or {}),
            active_position=active_position,
            active_positions=[active_position] if active_position is not None else [],
            payload={"bar_history": list(history_bars or []), "harness": True},
        )

    def evaluate_entry(self, runtime: PluginRuntimeState) -> OptionPluginHarnessResult:
        result = OptionPluginHarnessResult(lifecycle_events=self.lifecycle_events)
        signal = self.plugin.build_signal_pure(runtime)
        result.signal = signal
        if signal is None:
            return result
        entry_plan = self.plugin.build_entry_plan_pure(runtime, signal)
        result.entry_plan = entry_plan
        if entry_plan is None:
            reject = OrderReject(
                reason="no_entry_plan",
                intent_action="entry",
                strategy_id=runtime.strategy_id,
                symbol=runtime.symbol,
            )
            self.plugin.on_signal_rejected(
                runtime=runtime,
                signal=signal,
                entry_plan=None,
                selection=None,
                reject=reject,
            )
            result.rejected = True
            result.reject_reason = reject.reason
            return result
        self.plugin.on_signal_accepted(
            runtime=runtime,
            signal=signal,
            entry_plan=entry_plan,
            selection=None,
            payload={"source": "OptionPluginTestHarness", "stage": "entry_plan_built"},
        )
        selection = self.plugin.select_contract_pure(runtime, entry_plan)
        result.selection = selection
        if selection is None:
            self.plugin.on_entry_submit_blocked_pure(
                runtime=runtime,
                signal=signal,
                entry_plan=entry_plan,
                selection=None,
                reason="no_contract_selection",
                payload={"source": "OptionPluginTestHarness"},
            )
            result.rejected = True
            result.reject_reason = "no_contract_selection"
            return result
        self.plugin.on_signal_accepted(
            runtime=runtime,
            signal=signal,
            entry_plan=entry_plan,
            selection=selection,
            payload={"source": "OptionPluginTestHarness", "stage": "contract_selected"},
        )
        profile = self.plugin.build_execution_profile_pure(runtime, entry_plan, selection)
        result.execution_profile = profile
        if not isinstance(profile, ExecutionProfile):
            self.plugin.on_entry_submit_blocked_pure(
                runtime=runtime,
                signal=signal,
                entry_plan=entry_plan,
                selection=selection,
                reason="no_execution_profile",
                payload={"source": "OptionPluginTestHarness"},
            )
            result.rejected = True
            result.reject_reason = "no_execution_profile"
        return result
