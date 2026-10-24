from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from libs.strategy import DecisionEnvelope, Freshness, OrderStyle, make_decision_id

from .order_identity import ORDER_NAMESPACE, OrderKind, build_order_ref, new_trigger_id
from .ownership_service import (
    InMemoryPositionOwnershipService,
    OwnershipRecord,
    OwnershipState,
    PositionOwnershipService,
)
from .state_kernel import StateKernel, StateTransitionSink
from .runtime_store import InMemoryRuntimeStore, RuntimeLifecycle, RuntimeState, RuntimeStore


def _now_us() -> int:
    return int(time.time() * 1_000_000)


@dataclass(frozen=True)
class TradeIntent:
    strategy_id: str
    symbol: str
    target_qty: float
    side: str
    reason: str
    signal_price: Optional[float] = None
    trigger_id: Optional[str] = None
    order_kind: OrderKind = OrderKind.ENTRY
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CoordinationDecision:
    status: str
    runtime_state: RuntimeState
    ownership: OwnershipRecord
    envelope: Optional[DecisionEnvelope] = None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CoordinatorConfig:
    order_namespace: str = ORDER_NAMESPACE
    ttl_ms: int = 2_000
    dlb_ms: int = 500
    dry_run: bool = True


class RuntimeCoordinator:
    def __init__(
        self,
        *,
        runtime_store: Optional[RuntimeStore] = None,
        ownership_service: Optional[PositionOwnershipService] = None,
        config: Optional[CoordinatorConfig] = None,
        transition_sink: Optional[StateTransitionSink] = None,
        state_kernel: Optional[StateKernel] = None,
    ) -> None:
        self.runtime_store = runtime_store or InMemoryRuntimeStore()
        self.ownership_service = ownership_service or InMemoryPositionOwnershipService()
        self.config = config or CoordinatorConfig()
        self.transition_sink = transition_sink
        self.state_kernel = state_kernel or StateKernel()

    @staticmethod
    def _runtime_entity_id(strategy_id: str, symbol: str) -> str:
        return f"{str(strategy_id or '').strip()}:{str(symbol or '').strip().upper()}"

    async def _record_runtime_transition(
        self,
        *,
        strategy_id: str,
        symbol: str,
        before_state: RuntimeLifecycle | str,
        after_state: RuntimeLifecycle | str,
        event_type: str,
        reason: str,
        trigger_id: str | None = None,
        decision_id: str | None = None,
        order_ref: str | None = None,
        payload: Optional[dict[str, object]] = None,
    ) -> None:
        trace_id = str(decision_id or trigger_id or order_ref or self._runtime_entity_id(strategy_id, symbol))
        await self.state_kernel.apply_runtime_transition(
            sink=self.transition_sink,
            trace_id=trace_id,
            strategy_id=strategy_id,
            entity_id=self._runtime_entity_id(strategy_id, symbol),
            symbol=symbol,
            before_state=before_state,
            after_state=after_state,
            event_type=event_type,
            reason=reason,
            source_module="runtime_coordinator",
            trigger_id=trigger_id,
            decision_id=decision_id,
            order_ref=order_ref,
            payload=payload,
        )

    async def _record_runtime_transition_chain(
        self,
        *,
        strategy_id: str,
        symbol: str,
        transitions: list[tuple[RuntimeLifecycle | str, RuntimeLifecycle | str, str, str]],
        trigger_id: str | None = None,
        decision_id: str | None = None,
        order_ref: str | None = None,
        payload: Optional[dict[str, object]] = None,
    ) -> None:
        for before_state, after_state, event_type, reason in transitions:
            await self._record_runtime_transition(
                strategy_id=strategy_id,
                symbol=symbol,
                before_state=before_state,
                after_state=after_state,
                event_type=event_type,
                reason=reason,
                trigger_id=trigger_id,
                decision_id=decision_id,
                order_ref=order_ref,
                payload=payload,
            )

    async def preview_entry(self, intent: TradeIntent) -> CoordinationDecision:
        trigger_id = intent.trigger_id or new_trigger_id()
        symbol = str(intent.symbol or "").strip().upper()
        strategy_id = str(intent.strategy_id or "").strip()
        order_ref = build_order_ref(
            strategy_id=strategy_id,
            symbol=symbol,
            kind=intent.order_kind,
            trigger_id=trigger_id,
            namespace=self.config.order_namespace,
        )
        ownership = await self.ownership_service.get(symbol)
        if ownership.state == OwnershipState.QUARANTINED:
            return CoordinationDecision(
                status="QUARANTINED",
                runtime_state=RuntimeState(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    lifecycle=RuntimeLifecycle.QUARANTINE,
                    target_qty=float(intent.target_qty),
                    side=str(intent.side or "").strip().upper() or None,
                    active_trigger_id=trigger_id,
                    entry_order_ref=order_ref,
                    quarantine_reason=ownership.quarantine_reason,
                    last_synced_at_us=_now_us(),
                ),
                ownership=ownership,
                reasons=(ownership.quarantine_reason or "ownership_quarantined",),
                metadata={
                    "trigger_id": trigger_id,
                    "order_ref": order_ref,
                    "dry_run": self.config.dry_run,
                },
            )
        if ownership.state == OwnershipState.CLAIMED and ownership.owner_strategy_id not in {None, strategy_id}:
            return CoordinationDecision(
                status="OWNERSHIP_CONFLICT",
                runtime_state=RuntimeState(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    lifecycle=RuntimeLifecycle.FLAT,
                    target_qty=0.0,
                    side=None,
                    active_trigger_id=trigger_id,
                    entry_order_ref=order_ref,
                    last_error=f"owned_by:{ownership.owner_strategy_id}",
                    last_synced_at_us=_now_us(),
                ),
                ownership=ownership,
                reasons=(f"owned_by:{ownership.owner_strategy_id}",),
                metadata={
                    "trigger_id": trigger_id,
                    "order_ref": order_ref,
                    "dry_run": self.config.dry_run,
                },
            )
        runtime_state = RuntimeState(
            strategy_id=strategy_id,
            symbol=symbol,
            lifecycle=RuntimeLifecycle.PREVIEW,
            target_qty=float(intent.target_qty),
            side=str(intent.side or "").strip().upper() or None,
            active_trigger_id=trigger_id,
            entry_order_ref=order_ref,
            last_synced_at_us=_now_us(),
        )
        envelope = None
        status = "DRY_RUN_PREVIEW"
        if not self.config.dry_run:
            envelope = DecisionEnvelope(
                decision_id=make_decision_id(),
                strategy_id=strategy_id,
                symbol=symbol,
                ts_bar_end_us=_now_us(),
                target_qty=float(intent.target_qty),
                order_style=OrderStyle(kind="MKT"),
                freshness=Freshness(ttl_ms=int(self.config.ttl_ms), dlb_ms=int(self.config.dlb_ms)),
                extras={
                    "reason": intent.reason,
                    "trigger_id": trigger_id,
                    "order_ref": order_ref,
                    "signal_price": intent.signal_price,
                    "order_namespace": self.config.order_namespace,
                    "intent_source": "index_super_win",
                    **dict(intent.metadata or {}),
                },
            )
            status = "ENVELOPE_READY"
        await self._record_runtime_transition(
            strategy_id=strategy_id,
            symbol=symbol,
            before_state=RuntimeLifecycle.FLAT,
            after_state=RuntimeLifecycle.PREVIEW,
            event_type="preview_entry",
            reason=intent.reason,
            trigger_id=trigger_id,
            decision_id=envelope.decision_id if envelope is not None else None,
            order_ref=order_ref,
            payload={"dry_run": self.config.dry_run},
        )
        return CoordinationDecision(
            status=status,
            runtime_state=runtime_state,
            ownership=ownership,
            envelope=envelope,
            reasons=(intent.reason,),
            metadata={
                "trigger_id": trigger_id,
                "order_ref": order_ref,
                "dry_run": self.config.dry_run,
            },
        )

    async def mark_entry_submitted(self, intent: TradeIntent, *, trigger_id: str, order_ref: str) -> CoordinationDecision:
        symbol = str(intent.symbol or "").strip().upper()
        strategy_id = str(intent.strategy_id or "").strip()
        ownership = await self.ownership_service.get(symbol)
        runtime_state = await self.runtime_store.put_state(
            RuntimeState(
                strategy_id=strategy_id,
                symbol=symbol,
                lifecycle=RuntimeLifecycle.INTENT_SUBMITTED,
                target_qty=float(intent.target_qty),
                side=str(intent.side or "").strip().upper() or None,
                active_trigger_id=trigger_id,
                entry_order_ref=order_ref,
                last_synced_at_us=_now_us(),
            )
        )
        await self._record_runtime_transition(
            strategy_id=strategy_id,
            symbol=symbol,
            before_state=RuntimeLifecycle.PREVIEW,
            after_state=RuntimeLifecycle.INTENT_SUBMITTED,
            event_type="entry_submitted",
            reason=intent.reason,
            trigger_id=trigger_id,
            order_ref=order_ref,
            payload={"dry_run": self.config.dry_run},
        )
        return CoordinationDecision(
            status="INTENT_SUBMITTED",
            runtime_state=runtime_state,
            ownership=ownership,
            reasons=(intent.reason,),
            metadata={
                "trigger_id": trigger_id,
                "order_ref": order_ref,
                "dry_run": self.config.dry_run,
            },
        )

    async def update_protective_order_refs(
        self,
        *,
        strategy_id: str,
        symbol: str,
        order_refs: list[str] | tuple[str, ...],
    ) -> RuntimeState:
        symbol_u = str(symbol or "").strip().upper()
        strategy_u = str(strategy_id or "").strip()
        refs = tuple(str(ref).strip() for ref in list(order_refs or []) if str(ref).strip())
        current = await self.runtime_store.get_state(strategy_u, symbol_u)
        if current is None:
            next_state = RuntimeState(
                strategy_id=strategy_u,
                symbol=symbol_u,
                lifecycle=RuntimeLifecycle.FLAT,
                protective_order_refs=refs,
                last_synced_at_us=_now_us(),
            )
        else:
            next_state = current.evolve(
                protective_order_refs=refs,
                last_synced_at_us=_now_us(),
            )
        return await self.runtime_store.put_state(next_state)

    async def mark_pending_entry_from_order_update(
        self,
        *,
        strategy_id: str,
        symbol: str,
        order_ref: str | None,
        trigger_id: str | None,
    ) -> RuntimeState:
        symbol_u = str(symbol or "").strip().upper()
        strategy_u = str(strategy_id or "").strip()
        now_us = _now_us()
        current = await self.runtime_store.get_state(strategy_u, symbol_u)

        if current is not None and current.lifecycle in {
            RuntimeLifecycle.LIVE,
            RuntimeLifecycle.PENDING_EXIT,
            RuntimeLifecycle.QUARANTINE,
        }:
            return await self.runtime_store.put_state(current.evolve(last_synced_at_us=now_us))

        transitions: list[tuple[RuntimeLifecycle | str, RuntimeLifecycle | str, str, str]] = []
        if current is None:
            next_state = RuntimeState(
                strategy_id=strategy_u,
                symbol=symbol_u,
                lifecycle=RuntimeLifecycle.PENDING_ENTRY,
                active_trigger_id=trigger_id,
                entry_order_ref=str(order_ref or "").strip() or None,
                last_synced_at_us=now_us,
            )
            transitions.extend(
                [
                    (
                        RuntimeLifecycle.FLAT,
                        RuntimeLifecycle.INTENT_SUBMITTED,
                        "entry_submitted_backfilled",
                        "order_update:backfill_submitted_intent",
                    ),
                    (
                        RuntimeLifecycle.INTENT_SUBMITTED,
                        RuntimeLifecycle.PENDING_ENTRY,
                        "order_update_pending_entry",
                        "order_update:broker_acknowledged",
                    ),
                ]
            )
        else:
            next_state = current.evolve(
                lifecycle=RuntimeLifecycle.PENDING_ENTRY,
                active_trigger_id=trigger_id or current.active_trigger_id,
                entry_order_ref=str(order_ref or current.entry_order_ref or "").strip() or None,
                last_error=None,
                quarantine_reason=None,
                last_synced_at_us=now_us,
            )
            if current.lifecycle == RuntimeLifecycle.PREVIEW:
                transitions.extend(
                    [
                        (
                            RuntimeLifecycle.PREVIEW,
                            RuntimeLifecycle.INTENT_SUBMITTED,
                            "entry_submitted_backfilled",
                            "order_update:backfill_submitted_intent",
                        ),
                        (
                            RuntimeLifecycle.INTENT_SUBMITTED,
                            RuntimeLifecycle.PENDING_ENTRY,
                            "order_update_pending_entry",
                            "order_update:broker_acknowledged",
                        ),
                    ]
                )
            elif current.lifecycle == RuntimeLifecycle.INTENT_SUBMITTED:
                transitions.append(
                    (
                        RuntimeLifecycle.INTENT_SUBMITTED,
                        RuntimeLifecycle.PENDING_ENTRY,
                        "order_update_pending_entry",
                        "order_update:broker_acknowledged",
                    )
                )
            else:
                transitions.append(
                    (
                        current.lifecycle,
                        RuntimeLifecycle.PENDING_ENTRY,
                        "order_update_pending_entry",
                        "order_update:broker_acknowledged",
                    )
                )

        await self._record_runtime_transition_chain(
            strategy_id=strategy_u,
            symbol=symbol_u,
            transitions=transitions,
            trigger_id=next_state.active_trigger_id,
            order_ref=next_state.entry_order_ref,
        )
        persisted = await self.runtime_store.put_state(next_state)
        return persisted

    async def mark_live_from_broker(
        self,
        *,
        strategy_id: str,
        symbol: str,
        broker_qty: float,
        avg_px: Optional[float] = None,
        reason: str = "broker_position_detected",
    ) -> CoordinationDecision:
        symbol_u = str(symbol or "").strip().upper()
        strategy_u = str(strategy_id or "").strip()
        qty = float(broker_qty or 0.0)
        current = await self.runtime_store.get_state(strategy_u, symbol_u)
        ownership = await self.ownership_service.get(symbol_u)
        if self.config.dry_run:
            return await self.mark_quarantine(
                strategy_id=strategy_u,
                symbol=symbol_u,
                reason=f"{reason}:dry_run_broker_position",
                broker_qty=qty if abs(qty) > 1e-12 else None,
            )
        if abs(qty) <= 1e-12:
            return await self.mark_flat(strategy_id=strategy_u, symbol=symbol_u, reason=f"{reason}:flat")
        side = "LONG" if qty > 0 else "SHORT"
        if ownership.state == OwnershipState.CLAIMED and ownership.owner_strategy_id not in {None, strategy_u}:
            return await self.mark_quarantine(
                strategy_id=strategy_u,
                symbol=symbol_u,
                reason=f"{reason}:owned_by:{ownership.owner_strategy_id}",
                broker_qty=qty,
            )
        if current is None or current.lifecycle not in {RuntimeLifecycle.INTENT_SUBMITTED, RuntimeLifecycle.PENDING_ENTRY, RuntimeLifecycle.LIVE}:
            return await self.mark_quarantine(
                strategy_id=strategy_u,
                symbol=symbol_u,
                reason=f"{reason}:missing_submitted_intent",
                broker_qty=qty,
            )
        claimed = await self.ownership_service.claim(
            strategy_id=strategy_u,
            symbol=symbol_u,
            qty=abs(qty),
            trigger_id=current.active_trigger_id,
            order_namespace=self.config.order_namespace,
        )
        runtime_state = await self.runtime_store.put_state(
            current.evolve(
                lifecycle=RuntimeLifecycle.LIVE,
                live_qty=abs(qty),
                broker_qty=qty,
                side=side,
                last_error=None,
                quarantine_reason=None,
                last_synced_at_us=_now_us(),
                ownership_token=f"{strategy_u}:{symbol_u}:{current.active_trigger_id or 'live'}",
            )
        )
        await self._record_runtime_transition(
            strategy_id=strategy_u,
            symbol=symbol_u,
            before_state=current.lifecycle,
            after_state=RuntimeLifecycle.LIVE,
            event_type="broker_position_confirmed",
            reason=reason,
            trigger_id=current.active_trigger_id,
            order_ref=current.entry_order_ref,
            payload={"avg_px": avg_px, "broker_qty": qty},
        )
        return CoordinationDecision(
            status="LIVE_FROM_BROKER",
            runtime_state=runtime_state,
            ownership=claimed,
            reasons=(reason,),
            metadata={
                "avg_px": avg_px,
                "broker_qty": qty,
                "dry_run": self.config.dry_run,
            },
        )

    async def mark_quarantine(
        self,
        *,
        strategy_id: str,
        symbol: str,
        reason: str,
        broker_qty: Optional[float] = None,
    ) -> CoordinationDecision:
        symbol_u = str(symbol or "").strip().upper()
        strategy_u = str(strategy_id or "").strip()
        current = await self.runtime_store.get_state(strategy_u, symbol_u)
        ownership = await self.ownership_service.quarantine(
            symbol=symbol_u,
            reason=reason,
            broker_qty=float(broker_qty) if broker_qty is not None else None,
            owner_strategy_id=strategy_u if current is not None else None,
        )
        runtime_state = await self.runtime_store.put_state(
            RuntimeState(
                strategy_id=strategy_u,
                symbol=symbol_u,
                lifecycle=RuntimeLifecycle.QUARANTINE,
                target_qty=float(current.target_qty) if current is not None else 0.0,
                live_qty=abs(float(broker_qty or 0.0)),
                broker_qty=float(broker_qty) if broker_qty is not None else None,
                side=current.side if current is not None else None,
                active_trigger_id=current.active_trigger_id if current is not None else None,
                ownership_token=current.ownership_token if current is not None else None,
                entry_order_ref=current.entry_order_ref if current is not None else None,
                quarantine_reason=reason,
                last_error=reason,
                last_synced_at_us=_now_us(),
            )
        )
        before_state = current.lifecycle if current is not None else RuntimeLifecycle.FLAT
        await self._record_runtime_transition(
            strategy_id=strategy_u,
            symbol=symbol_u,
            before_state=before_state,
            after_state=RuntimeLifecycle.QUARANTINE,
            event_type="quarantine_applied",
            reason=reason,
            trigger_id=current.active_trigger_id if current is not None else None,
            order_ref=current.entry_order_ref if current is not None else None,
            payload={"broker_qty": broker_qty},
        )
        return CoordinationDecision(
            status="QUARANTINED",
            runtime_state=runtime_state,
            ownership=ownership,
            reasons=(reason,),
            metadata={
                "broker_qty": broker_qty,
                "dry_run": self.config.dry_run,
            },
        )

    async def mark_flat(self, *, strategy_id: str, symbol: str, reason: str) -> CoordinationDecision:
        symbol_u = str(symbol or "").strip().upper()
        strategy_u = str(strategy_id or "").strip()
        previous = await self.runtime_store.get_state(strategy_u, symbol_u)
        ownership = await self.ownership_service.release(
            strategy_id=strategy_u,
            symbol=symbol_u,
            force=False,
        )
        runtime_state = await self.runtime_store.put_state(
            RuntimeState(
                strategy_id=strategy_u,
                symbol=symbol_u,
                lifecycle=RuntimeLifecycle.FLAT,
                target_qty=0.0,
                live_qty=0.0,
                last_synced_at_us=_now_us(),
                last_error=reason,
            )
        )
        before_state = previous.lifecycle if previous is not None else RuntimeLifecycle.PENDING_EXIT
        await self._record_runtime_transition(
            strategy_id=strategy_u,
            symbol=symbol_u,
            before_state=before_state,
            after_state=RuntimeLifecycle.FLAT,
            event_type="flat_recorded",
            reason=reason,
            payload={"dry_run": self.config.dry_run},
        )
        return CoordinationDecision(
            status="FLAT_RECORDED",
            runtime_state=runtime_state,
            ownership=ownership,
            reasons=(reason,),
            metadata={"dry_run": self.config.dry_run},
        )
