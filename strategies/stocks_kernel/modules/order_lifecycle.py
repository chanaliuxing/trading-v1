from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from services.strategy_runtime_v2.state_kernel import (
    OrderLifecycleState,
    StateKernel,
    StateTransitionSink,
)


TERMINAL_ORDER_STATES = {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "FAILED", "FILLED"}


@dataclass(frozen=True)
class OrderLifecycleRecord:
    order_ref: str
    symbol: str
    position_lot_id: str | None
    state: str
    created_ts_epoch_s: float
    updated_ts_epoch_s: float
    submit_ts_epoch_s: float | None = None
    ack_ts_epoch_s: float | None = None
    terminal_ts_epoch_s: float | None = None
    broker_order_id: str | None = None
    perm_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PendingConvergenceBreach:
    order_ref: str
    symbol: str
    state: str
    age_sec: float
    timeout_sec: float
    reason: str


class OrderLifecycleModule:
    """
    Track order lifecycle and freeze entry lanes when convergence breaches happen.
    """

    def __init__(
        self,
        *,
        pending_timeout_sec: float = 60.0,
        transition_sink: StateTransitionSink | None = None,
        state_kernel: StateKernel | None = None,
        strategy_id: str | None = None,
    ) -> None:
        self._pending_timeout_sec = max(15.0, float(pending_timeout_sec or 60.0))
        self._records: dict[str, OrderLifecycleRecord] = {}
        self._entry_freezes: dict[str, str] = {}
        self._transition_sink = transition_sink
        self._state_kernel = state_kernel or StateKernel()
        self._strategy_id = str(strategy_id or "").strip() or "stocks_kernel"

    def pending_timeout_sec(self) -> float:
        return self._pending_timeout_sec

    def should_freeze_entry(self, *, symbol: str) -> bool:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return False
        return symbol_u in self._entry_freezes

    def freeze_reason(self, *, symbol: str) -> str | None:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return None
        return self._entry_freezes.get(symbol_u)

    def mark_sync_issue(self, *, symbol: str, reason: str) -> None:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return
        self._entry_freezes[symbol_u] = str(reason or "broker_truth_sync_issue")

    def clear_freeze(self, *, symbol: str) -> bool:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return False
        return self._entry_freezes.pop(symbol_u, None) is not None

    def register_created(
        self,
        *,
        order_ref: str,
        symbol: str,
        position_lot_id: str | None,
        now_ts_epoch_s: float,
    ) -> None:
        order_ref_v = str(order_ref or "").strip()
        symbol_u = str(symbol or "").strip().upper()
        if not order_ref_v or not symbol_u:
            return
        now_ts = float(now_ts_epoch_s or 0.0)
        self._records[order_ref_v] = OrderLifecycleRecord(
            order_ref=order_ref_v,
            symbol=symbol_u,
            position_lot_id=str(position_lot_id or "").strip() or None,
            state="CREATED",
            created_ts_epoch_s=now_ts,
            updated_ts_epoch_s=now_ts,
        )
        self._record_transition(
            order_ref=order_ref_v,
            symbol=symbol_u,
            before_state=None,
            after_state=OrderLifecycleState.CREATED,
            event_type="order_created",
            reason="created",
            payload={"position_lot_id": str(position_lot_id or "").strip() or None},
        )

    def mark_submitted(self, *, order_ref: str, now_ts_epoch_s: float) -> None:
        current = self._records.get(str(order_ref or "").strip())
        self._patch(order_ref=order_ref, state="SUBMITTED", submit_ts_epoch_s=float(now_ts_epoch_s or 0.0))
        self._record_transition(
            order_ref=order_ref,
            symbol=(current.symbol if current is not None else ""),
            before_state=(current.state if current is not None else None),
            after_state=OrderLifecycleState.SUBMITTED,
            event_type="order_submitted",
            reason="submitted",
        )

    def mark_accepted(
        self,
        *,
        order_ref: str,
        now_ts_epoch_s: float,
        broker_order_id: str | None = None,
        perm_id: int | None = None,
    ) -> None:
        updates: dict[str, Any] = {
            "state": "ACCEPTED",
            "ack_ts_epoch_s": float(now_ts_epoch_s or 0.0),
            "broker_order_id": str(broker_order_id or "").strip() or None,
            "perm_id": int(perm_id) if perm_id is not None else None,
        }
        current = self._records.get(str(order_ref or "").strip())
        self._patch(order_ref=order_ref, **updates)
        self._record_transition(
            order_ref=order_ref,
            symbol=(current.symbol if current is not None else ""),
            before_state=(current.state if current is not None else None),
            after_state=OrderLifecycleState.ACCEPTED,
            event_type="order_accepted",
            reason="accepted",
            payload={
                "broker_order_id": str(broker_order_id or "").strip() or None,
                "perm_id": int(perm_id) if perm_id is not None else None,
            },
        )

    def mark_fill(self, *, order_ref: str, now_ts_epoch_s: float) -> None:
        current = self._records.get(str(order_ref or "").strip())
        self._patch(
            order_ref=order_ref,
            state="FILLED",
            terminal_ts_epoch_s=float(now_ts_epoch_s or 0.0),
            reason="trusted_fill",
        )
        self._record_transition(
            order_ref=order_ref,
            symbol=(current.symbol if current is not None else ""),
            before_state=(current.state if current is not None else None),
            after_state=OrderLifecycleState.FILLED,
            event_type="order_filled",
            reason="trusted_fill",
        )

    def mark_terminal(self, *, order_ref: str, state: str, now_ts_epoch_s: float) -> None:
        state_u = str(state or "").strip().upper() or "TERMINAL"
        current = self._records.get(str(order_ref or "").strip())
        self._patch(
            order_ref=order_ref,
            state=state_u,
            terminal_ts_epoch_s=float(now_ts_epoch_s or 0.0),
            reason=f"terminal:{state_u.lower()}",
        )
        target = self._to_order_lifecycle_state(state_u)
        if target is not None:
            self._record_transition(
                order_ref=order_ref,
                symbol=(current.symbol if current is not None else ""),
                before_state=(current.state if current is not None else None),
                after_state=target,
                event_type=f"order_{target.value}",
                reason=f"terminal:{target.value}",
            )

    def sweep_breaches(self, *, now_ts_epoch_s: float) -> tuple[PendingConvergenceBreach, ...]:
        now_ts = float(now_ts_epoch_s or 0.0)
        breaches: list[PendingConvergenceBreach] = []
        for record in list(self._records.values()):
            if record.state in TERMINAL_ORDER_STATES:
                continue
            ref_ts = (
                float(record.ack_ts_epoch_s or 0.0)
                or float(record.submit_ts_epoch_s or 0.0)
                or float(record.created_ts_epoch_s or 0.0)
            )
            if ref_ts <= 0.0 or now_ts <= ref_ts:
                continue
            age_sec = max(0.0, now_ts - ref_ts)
            if age_sec < self._pending_timeout_sec:
                continue
            reason = "ack_not_converged_timeout" if record.state == "ACCEPTED" else "pending_not_converged_timeout"
            self._entry_freezes[record.symbol] = reason
            breaches.append(
                PendingConvergenceBreach(
                    order_ref=record.order_ref,
                    symbol=record.symbol,
                    state=record.state,
                    age_sec=age_sec,
                    timeout_sec=self._pending_timeout_sec,
                    reason=reason,
                )
            )
        return tuple(breaches)

    def pending_non_terminal_orders(
        self,
        *,
        symbols: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[int, tuple[str, ...]]:
        symbol_filter = {
            str(item or "").strip().upper()
            for item in tuple(symbols or ())
            if str(item or "").strip()
        }
        refs: list[str] = []
        for record in self._records.values():
            state = str(record.state or "").strip().upper()
            if state in TERMINAL_ORDER_STATES:
                continue
            if symbol_filter and str(record.symbol or "").strip().upper() not in symbol_filter:
                continue
            refs.append(str(record.order_ref or "").strip())
        refs = [ref for ref in refs if ref]
        refs_sorted = tuple(sorted(set(refs)))
        return len(refs_sorted), refs_sorted

    def non_terminal_records(
        self,
        *,
        symbols: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[OrderLifecycleRecord, ...]:
        symbol_filter = {
            str(item or "").strip().upper()
            for item in tuple(symbols or ())
            if str(item or "").strip()
        }
        records: list[OrderLifecycleRecord] = []
        for record in self._records.values():
            state = str(record.state or "").strip().upper()
            if state in TERMINAL_ORDER_STATES:
                continue
            if symbol_filter and str(record.symbol or "").strip().upper() not in symbol_filter:
                continue
            records.append(record)
        return tuple(sorted(records, key=lambda item: str(item.order_ref or "")))

    def _patch(self, *, order_ref: str, **updates: Any) -> None:
        order_ref_v = str(order_ref or "").strip()
        if not order_ref_v:
            return
        current = self._records.get(order_ref_v)
        if current is None:
            return
        updates["updated_ts_epoch_s"] = float(updates.get("updated_ts_epoch_s") or updates.get("terminal_ts_epoch_s") or updates.get("ack_ts_epoch_s") or updates.get("submit_ts_epoch_s") or current.updated_ts_epoch_s)
        self._records[order_ref_v] = replace(current, **updates)

    @staticmethod
    def _to_order_lifecycle_state(state: str | None) -> OrderLifecycleState | None:
        raw = str(state or "").strip().upper()
        if not raw:
            return None
        mapping = {
            "CREATED": OrderLifecycleState.CREATED,
            "SUBMITTED": OrderLifecycleState.SUBMITTED,
            "ACCEPTED": OrderLifecycleState.ACCEPTED,
            "FILLED": OrderLifecycleState.FILLED,
            "CANCELLED": OrderLifecycleState.CANCELLED,
            "CANCELED": OrderLifecycleState.CANCELLED,
            "REJECTED": OrderLifecycleState.REJECTED,
            "EXPIRED": OrderLifecycleState.EXPIRED,
            "FAILED": OrderLifecycleState.FAILED,
        }
        return mapping.get(raw)

    def _record_transition(
        self,
        *,
        order_ref: str,
        symbol: str,
        before_state: str | None,
        after_state: OrderLifecycleState,
        event_type: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._transition_sink is None:
            return
        before = self._to_order_lifecycle_state(before_state)
        if before is None:
            before = OrderLifecycleState.CREATED
            if after_state == OrderLifecycleState.CREATED:
                return
        trace_id = str(order_ref or "").strip() or f"order:{str(symbol or '').strip().upper()}"
        entity_id = str(order_ref or "").strip() or f"{str(symbol or '').strip().upper()}:order"
        import asyncio

        coro = self._state_kernel.apply_order_transition(
            sink=self._transition_sink,
            trace_id=trace_id,
            strategy_id=self._strategy_id,
            entity_id=entity_id,
            symbol=str(symbol or "").strip().upper() or None,
            order_ref=str(order_ref or "").strip() or None,
            before_state=before,
            after_state=after_state,
            event_type=event_type,
            reason=reason,
            source_module="order_lifecycle_module",
            payload=dict(payload or {}),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        loop.create_task(coro)
