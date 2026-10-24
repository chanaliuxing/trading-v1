from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from strategies.options_kernel.types import EntryPlan, PluginRuntimeState, Selection


@dataclass(frozen=True)
class ContractSelectionContext:
    runtime: PluginRuntimeState
    entry_plan: EntryPlan
    extras: Dict[str, Any] = field(default_factory=dict)


class BaseContractPolicy(Protocol):
    policy_id: str

    def select_contract(self, ctx: ContractSelectionContext) -> Optional[Selection]:
        ...
