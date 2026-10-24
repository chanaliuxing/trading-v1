from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from strategies.options_kernel.types import ExitPlan, PluginRuntimeState


@dataclass(frozen=True)
class PositionContext:
    runtime: PluginRuntimeState
    extras: Dict[str, Any] = field(default_factory=dict)


class BasePositionPolicy(Protocol):
    policy_id: str

    def build_exit_plan(self, ctx: PositionContext) -> Optional[ExitPlan]:
        ...

    def build_exit_plans(self, ctx: PositionContext) -> List[ExitPlan]:
        ...
