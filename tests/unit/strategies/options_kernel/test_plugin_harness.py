from __future__ import annotations

from datetime import datetime

from strategies.options_kernel.plugin import BaseOptionPlugin
from strategies.options_kernel.test_harness import OptionPluginTestHarness
from strategies.options_kernel.types import EntryPlan, ExecutionProfile, OrderReject, PluginRuntimeState, Selection, Signal


class _HarnessPlugin(BaseOptionPlugin):
    component_name = "harness_plugin"

    def __init__(self, *, reject_contract: bool = False) -> None:
        self.reject_contract = reject_contract
        self.events: list[tuple[str, str]] = []

    def build_signal_pure(self, runtime: PluginRuntimeState):
        if int(runtime.features.get("bars") or 0) < 3:
            return None
        return Signal(ts=runtime.now, side="CALL", signal_kind="harness")

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal):
        return EntryPlan(
            ts=runtime.now,
            side=signal.side,
            signal_kind=signal.signal_kind,
            structure_stop=1.0,
            target_1r=2.0,
            target_1p2r=2.4,
            r_per_share=1.0,
            contracts=1,
        )

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan):
        if self.reject_contract:
            return None
        return Selection(
            opt_symbol="OPT:QQQ:20260515:500:C",
            expiry="20260515",
            strike=500.0,
            side=entry_plan.side,
            bid=1.0,
            ask=1.1,
            mid=1.05,
        )

    def build_execution_profile_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan, selection):
        return ExecutionProfile(profile_id="harness", market_data_profile_id="md", timeout_bars=3)

    def on_signal_accepted(self, *, runtime, signal, entry_plan, selection=None, payload=None):
        self.events.append(("accepted", str((payload or {}).get("stage") or "")))

    def on_signal_rejected(self, *, runtime, signal, entry_plan, selection=None, reject: OrderReject):
        self.events.append(("rejected", reject.reason))


def test_option_plugin_harness_accepts_entry_path():
    plugin = _HarnessPlugin()
    harness = OptionPluginTestHarness(plugin, strategy_id="harness_001", symbol="QQQ")
    runtime = harness.runtime(
        now=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
        spot=500.0,
        features={"bars": 5},
    )

    result = harness.evaluate_entry(runtime)

    assert result.rejected is False
    assert result.selection is not None
    assert plugin.events == [("accepted", "entry_plan_built"), ("accepted", "contract_selected")]


def test_option_plugin_harness_surfaces_contract_reject_lifecycle():
    plugin = _HarnessPlugin(reject_contract=True)
    harness = OptionPluginTestHarness(plugin, strategy_id="harness_001", symbol="QQQ")
    runtime = harness.runtime(
        now=datetime.fromisoformat("2026-05-06T10:00:00-04:00"),
        spot=500.0,
        features={"bars": 5},
    )

    result = harness.evaluate_entry(runtime)

    assert result.rejected is True
    assert result.reject_reason == "no_contract_selection"
    assert ("rejected", "no_contract_selection") in plugin.events
