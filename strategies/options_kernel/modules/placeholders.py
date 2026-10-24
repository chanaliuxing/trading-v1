from __future__ import annotations

from typing import Optional

from strategies.options_kernel.types import EntryPlan, ExecutionProfile, ExitPlan, Selection, Signal

from .contract_policy import BaseContractPolicy, ContractSelectionContext
from .execution_policy import BaseExecutionPolicy, ExecutionContext
from .position_policy import BasePositionPolicy, PositionContext
from .signal import BaseSignalModule, SignalContext


class PlaceholderSignalModule(BaseSignalModule):
    def __init__(self, module_id: str) -> None:
        self.module_id = module_id

    def build_signal(self, ctx: SignalContext) -> Optional[Signal]:
        return None

    def build_entry_plan(self, ctx: SignalContext, signal: Signal) -> Optional[EntryPlan]:
        return None

    def reset_for_new_day(self, ctx: SignalContext) -> None:
        return None


class PlaceholderContractPolicy(BaseContractPolicy):
    def __init__(self, policy_id: str) -> None:
        self.policy_id = policy_id

    def select_contract(self, ctx: ContractSelectionContext) -> Optional[Selection]:
        return None


class PlaceholderExecutionPolicy(BaseExecutionPolicy):
    def __init__(self, policy_id: str) -> None:
        self.policy_id = policy_id

    def build_execution_profile(self, ctx: ExecutionContext) -> ExecutionProfile:
        raise NotImplementedError("PlaceholderExecutionPolicy cannot build execution profiles yet")


class PlaceholderPositionPolicy(BasePositionPolicy):
    def __init__(self, policy_id: str) -> None:
        self.policy_id = policy_id

    def build_exit_plan(self, ctx: PositionContext) -> Optional[ExitPlan]:
        return None

    def build_exit_plans(self, ctx: PositionContext) -> list[ExitPlan]:
        return []
