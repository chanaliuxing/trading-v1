from __future__ import annotations

from dataclasses import replace

from strategies.stocks_kernel.types import KernelExecutionEvent


class InMemoryExecutionAuditTrail:
    """
    Minimal append-only audit collector for kernel development.

    This keeps observability scoped to the new kernel and can be replaced
    later by the async file writer without changing execution semantics.
    """

    def __init__(self) -> None:
        self._events: list[KernelExecutionEvent] = []
        self._sinks: list = []

    def add_sink(self, sink) -> None:
        if sink is not None:
            self._sinks.append(sink)

    def record(self, event: KernelExecutionEvent) -> KernelExecutionEvent:
        stored = replace(event)
        self._events.append(stored)
        for sink in list(self._sinks):
            sink(stored)
        return stored

    def list_events(
        self,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        position_lot_id: str | None = None,
    ) -> list[KernelExecutionEvent]:
        out: list[KernelExecutionEvent] = []
        strategy_id_v = str(strategy_id or "").strip() or None
        symbol_v = str(symbol or "").strip().upper() or None
        lot_id_v = str(position_lot_id or "").strip() or None
        for event in self._events:
            if strategy_id_v and event.strategy_id != strategy_id_v:
                continue
            if symbol_v and event.symbol != symbol_v:
                continue
            if lot_id_v and event.position_lot_id != lot_id_v:
                continue
            out.append(replace(event))
        return out
