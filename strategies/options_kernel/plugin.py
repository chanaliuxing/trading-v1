from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

from libs.strategy.base import StrategyAlgorithm, StrategyContext, StrategyParams

from .types import (
    EntryPlan,
    ExecutionProfile,
    ExitPlan,
    OrderAck,
    OrderReject,
    PluginRuntimeState,
    Selection,
    Signal,
)

if TYPE_CHECKING:
    from .base import OptionKernel


class OptionPlugin(Protocol):
    component_name: str

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def build_delegate(self, ctx: StrategyContext, params: StrategyParams) -> StrategyAlgorithm:
        ...

    def build_signal(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> Optional[Signal]:
        ...

    def build_entry_plan(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], signal: Signal) -> Optional[EntryPlan]:
        ...

    def select_contract(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], entry_plan: EntryPlan) -> Optional[Selection]:
        ...

    def build_execution_profile(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        ...

    def build_exit_plan(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        market_state: Dict[str, Any],
    ) -> Optional[ExitPlan]:
        ...

    async def maybe_enter(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        ...

    def select_quote(self, strategy: StrategyAlgorithm, req: Dict[str, Any]) -> Optional[Selection]:
        ...

    async def sync_and_manage_position(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        ...

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        ...

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        ...

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        ...

    def build_execution_profile_pure(
        self,
        runtime: PluginRuntimeState,
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        ...

    def build_exit_plan_pure(self, runtime: PluginRuntimeState) -> Optional[ExitPlan]:
        ...

    def build_exit_plans_pure(self, runtime: PluginRuntimeState) -> list[ExitPlan]:
        ...

    def on_entry_submitted_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        position_lot_id: str,
    ) -> None:
        ...

    def on_signal_accepted(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: Optional[EntryPlan],
        selection: Optional[Selection] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ...

    def on_signal_rejected(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: Optional[EntryPlan],
        selection: Optional[Selection] = None,
        reject: OrderReject,
    ) -> None:
        ...

    def on_order_submitted(
        self,
        *,
        runtime: PluginRuntimeState,
        ack: OrderAck,
        signal: Optional[Signal] = None,
        entry_plan: Optional[EntryPlan] = None,
        exit_plan: Optional[ExitPlan] = None,
        selection: Optional[Selection] = None,
    ) -> None:
        ...

    def on_fill(self, *, runtime: PluginRuntimeState, fill: Dict[str, Any]) -> None:
        ...

    def on_position_closed(
        self,
        *,
        runtime: PluginRuntimeState,
        position_lot_id: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ...

    async def initialize_pure(
        self,
        ctx: StrategyContext,
        params: StrategyParams,
        kernel: "OptionKernel",
    ) -> None:
        ...

    async def shutdown_pure(
        self,
        ctx: StrategyContext,
        params: StrategyParams,
        kernel: "OptionKernel",
        reason: str,
    ) -> None:
        ...


class BaseOptionPlugin:
    component_name = "option_plugin"
    kernel_can_submit = False
    supports_pure_kernel = False
    assembly_adapter_mode = False

    def apply_default_extras(self, extras: Dict[str, Any]) -> Dict[str, Any]:
        return dict(extras or {})

    def build_delegate(self, ctx: StrategyContext, params: StrategyParams) -> StrategyAlgorithm:
        raise NotImplementedError

    def build_signal(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> Optional[Signal]:
        return None

    def build_entry_plan(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], signal: Signal) -> Optional[EntryPlan]:
        return None

    def select_contract(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any], entry_plan: EntryPlan) -> Optional[Selection]:
        return None

    def build_execution_profile(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        raise NotImplementedError

    def build_exit_plan(
        self,
        strategy: StrategyAlgorithm,
        ctx: Dict[str, Any],
        market_state: Dict[str, Any],
    ) -> Optional[ExitPlan]:
        return None

    async def maybe_enter(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        return False

    def select_quote(self, strategy: StrategyAlgorithm, req: Dict[str, Any]) -> Optional[Selection]:
        return None

    async def sync_and_manage_position(self, strategy: StrategyAlgorithm, ctx: Dict[str, Any]) -> bool:
        return False

    def build_signal_pure(self, runtime: PluginRuntimeState) -> Optional[Signal]:
        return None

    def build_entry_plan_pure(self, runtime: PluginRuntimeState, signal: Signal) -> Optional[EntryPlan]:
        return None

    def select_contract_pure(self, runtime: PluginRuntimeState, entry_plan: EntryPlan) -> Optional[Selection]:
        return None

    def build_execution_profile_pure(
        self,
        runtime: PluginRuntimeState,
        entry_plan: EntryPlan,
        selection: Optional[Selection],
    ) -> ExecutionProfile:
        raise NotImplementedError

    def build_exit_plan_pure(self, runtime: PluginRuntimeState) -> Optional[ExitPlan]:
        return None

    def build_exit_plans_pure(self, runtime: PluginRuntimeState) -> list[ExitPlan]:
        plan = self.build_exit_plan_pure(runtime)
        return [plan] if plan is not None else []

    def on_entry_submitted_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Selection,
        position_lot_id: str,
    ) -> None:
        # The kernel now emits the standard on_order_submitted hook directly.
        # Keep this legacy hook as a compatibility no-op for plugins that still
        # override it for local state updates.
        return None

    def on_entry_submit_blocked_pure(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: EntryPlan,
        selection: Optional[Selection],
        reason: str,
        payload: Dict[str, Any],
    ) -> None:
        self.on_signal_rejected(
            runtime=runtime,
            signal=signal,
            entry_plan=entry_plan,
            selection=selection,
            reject=OrderReject(
                reason=str(reason or "entry_submit_blocked"),
                intent_action="entry",
                strategy_id=runtime.strategy_id,
                symbol=str(getattr(selection, "opt_symbol", "") or runtime.symbol),
                stage=str((payload or {}).get("result_stage") or ""),
                reasons=[str(item) for item in list((payload or {}).get("result_reasons") or [])],
                position_lot_id=str((payload or {}).get("position_lot_id") or ""),
                payload=dict(payload or {}),
            ),
        )
        return None

    def on_signal_accepted(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: Optional[EntryPlan],
        selection: Optional[Selection] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        return None

    def on_signal_rejected(
        self,
        *,
        runtime: PluginRuntimeState,
        signal: Optional[Signal],
        entry_plan: Optional[EntryPlan],
        selection: Optional[Selection] = None,
        reject: OrderReject,
    ) -> None:
        return None

    def on_order_submitted(
        self,
        *,
        runtime: PluginRuntimeState,
        ack: OrderAck,
        signal: Optional[Signal] = None,
        entry_plan: Optional[EntryPlan] = None,
        exit_plan: Optional[ExitPlan] = None,
        selection: Optional[Selection] = None,
    ) -> None:
        return None

    def on_fill(self, *, runtime: PluginRuntimeState, fill: Dict[str, Any]) -> None:
        return None

    def on_position_closed(
        self,
        *,
        runtime: PluginRuntimeState,
        position_lot_id: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        return None

    async def initialize_pure(
        self,
        ctx: StrategyContext,
        params: StrategyParams,
        kernel: "OptionKernel",
    ) -> None:
        return None

    async def shutdown_pure(
        self,
        ctx: StrategyContext,
        params: StrategyParams,
        kernel: "OptionKernel",
        reason: str,
    ) -> None:
        return None
