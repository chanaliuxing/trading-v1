from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from strategies.options_kernel.types import EntryPlan, PluginRuntimeState, Signal


@dataclass(frozen=True)
class SignalContext:
    runtime: PluginRuntimeState
    extras: Dict[str, Any] = field(default_factory=dict)


class BaseSignalModule(Protocol):
    module_id: str

    def build_signal(self, ctx: SignalContext) -> Optional[Signal]:
        ...

    def build_entry_plan(self, ctx: SignalContext, signal: Signal) -> Optional[EntryPlan]:
        ...

    def reset_for_new_day(self, ctx: SignalContext) -> None:
        ...
