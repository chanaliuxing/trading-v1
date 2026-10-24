from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol

from strategies.options_kernel.types import EntryPlan, ExecutionProfile, PluginRuntimeState, Selection


@dataclass(frozen=True)
class ExecutionContext:
    runtime: PluginRuntimeState
    entry_plan: EntryPlan
    selection: Selection | None
    extras: Dict[str, Any] = field(default_factory=dict)


class BaseExecutionPolicy(Protocol):
    policy_id: str

    def build_execution_profile(self, ctx: ExecutionContext) -> ExecutionProfile:
        ...
