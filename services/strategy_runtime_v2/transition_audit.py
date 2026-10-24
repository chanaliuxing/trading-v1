from __future__ import annotations

from typing import Callable, Optional

from libs.obs.audit_writer import LocalAuditWriter

from .state_transition_event import StateTransitionEvent


class LocalAuditStateTransitionSink:
    def __init__(
        self,
        *,
        audit_writer: LocalAuditWriter,
        component: str = "state_kernel",
        domain: str = "state",
    ) -> None:
        self.audit_writer = audit_writer
        self.component = str(component or "state_kernel").strip() or "state_kernel"
        self.domain = str(domain or "state").strip() or "state"

    async def record(self, event: StateTransitionEvent) -> None:
        self.audit_writer.emit(
            domain=self.domain,
            component=self.component,
            stage=f"{event.state_machine}_transition",
            severity="info",
            strategy_id=event.strategy_id,
            strategy_run_id=event.strategy_run_id,
            trigger_id=event.trigger_id,
            decision_id=event.decision_id,
            trace_id=event.trace_id,
            symbol=event.symbol,
            status=event.after_state,
            reason=event.reason,
            payload=event.to_dict(),
        )


class CallbackStateTransitionSink:
    def __init__(self, callback: Callable[[StateTransitionEvent], None]) -> None:
        self.callback = callback

    async def record(self, event: StateTransitionEvent) -> None:
        self.callback(event)


def build_local_audit_transition_sink(
    audit_writer: Optional[LocalAuditWriter],
    *,
    component: str = "state_kernel",
    domain: str = "state",
):
    if audit_writer is None:
        return None
    return LocalAuditStateTransitionSink(audit_writer=audit_writer, component=component, domain=domain)
