from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from libs.logger import get_logger
from services.exec.execution_state_model import FlattenLifecycleState
from services.orders.truth import OrderTruthService
from services.strategy_runtime_v2.state_kernel import StateKernel, StateTransitionSink

from .manual_intervention_lock import ManualInterventionLockStore

logger = get_logger(__name__)


def _merge_note_json(raw_note: Any, updates: Dict[str, Any]) -> str:
    base: Dict[str, Any] = {}
    if isinstance(raw_note, str) and raw_note.strip():
        try:
            parsed = json.loads(raw_note)
            if isinstance(parsed, dict):
                base = parsed
        except Exception:
            base = {}
    elif isinstance(raw_note, dict):
        base = dict(raw_note)
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged = dict(base.get(key) or {})
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value
    return json.dumps(base, ensure_ascii=False)


def _load_note_obj(raw_note: Any) -> Dict[str, Any]:
    if isinstance(raw_note, dict):
        return dict(raw_note)
    if isinstance(raw_note, str) and raw_note.strip():
        try:
            parsed = json.loads(raw_note)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _normalize_flatten_state(value: Any) -> FlattenLifecycleState:
    raw = str(value or "").strip().lower() or FlattenLifecycleState.CREATED.value
    return FlattenLifecycleState(raw)


def _fallback_flatten_before_state(target_state: Any) -> FlattenLifecycleState:
    target = _normalize_flatten_state(target_state)
    if target in {FlattenLifecycleState.PENDING, FlattenLifecycleState.CONFIRMED}:
        return FlattenLifecycleState.SUBMITTED
    if target in {
        FlattenLifecycleState.REJECTED,
        FlattenLifecycleState.CANCELLED,
        FlattenLifecycleState.FAILED,
        FlattenLifecycleState.QUARANTINED,
    }:
        return FlattenLifecycleState.SUBMITTED
    return FlattenLifecycleState.CREATED


class FlattenController:
    """
    Minimal flatten lifecycle controller.

    Phase 2 goal is to centralize flatten note persistence, completion resolution,
    and flatten_action lock release without rewriting the whole execution stack at once.
    """

    def __init__(
        self,
        *,
        pg: Any,
        order_truth_service: Optional[OrderTruthService] = None,
        manual_intervention_lock_store: Optional[ManualInterventionLockStore] = None,
        transition_sink: Optional[StateTransitionSink] = None,
        state_kernel: Optional[StateKernel] = None,
    ) -> None:
        self.pg = pg
        self.order_truth_service = order_truth_service
        self.manual_intervention_lock_store = manual_intervention_lock_store
        self.transition_sink = transition_sink
        self.state_kernel = state_kernel or StateKernel()

    async def _record_flatten_transition(
        self,
        *,
        resolved_order: Dict[str, Any],
        before_state: Any,
        after_state: Any,
        event_type: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        strategy_id = str(resolved_order.get("strategy_id") or "").strip() or "unknown_strategy"
        symbol = str(resolved_order.get("symbol") or "").strip().upper() or None
        order_ref = str(resolved_order.get("order_ref") or "").strip() or None
        order_id = str(resolved_order.get("order_id") or "").strip() or None
        note_obj = _load_note_obj(resolved_order.get("note"))
        trace_obj = note_obj.get("trace") if isinstance(note_obj.get("trace"), dict) else {}
        trace_id = (
            str(trace_obj.get("trace_id") or "").strip()
            or str(note_obj.get("decision_id") or "").strip()
            or order_ref
            or order_id
            or f"flatten:{strategy_id}:{symbol or 'UNKNOWN'}"
        )
        entity_id = order_id or order_ref or f"{strategy_id}:{symbol or 'UNKNOWN'}:flatten"
        await self.state_kernel.apply_flatten_transition(
            sink=self.transition_sink,
            trace_id=trace_id,
            strategy_id=strategy_id,
            entity_id=entity_id,
            symbol=symbol,
            order_ref=order_ref,
            before_state=_normalize_flatten_state(before_state),
            after_state=_normalize_flatten_state(after_state),
            event_type=event_type,
            reason=reason,
            source_module="flatten_controller",
            payload=payload,
        )

    @staticmethod
    def inject_created_lifecycle(*, note_obj: Dict[str, Any], envelope_extras: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        payload = dict(note_obj or {})
        extras = dict(envelope_extras or {})
        if bool(extras.get("flatten_action")):
            payload["flatten_lifecycle"] = {
                "state": "created",
                "created_at": datetime.utcnow().isoformat(),
                "close_only": bool(extras.get("close_only")),
            }
        return payload

    async def mark_submitted_by_idempotency(
        self,
        *,
        idempotency_key: str,
        order_id: int,
        close_only: bool,
    ) -> Optional[str]:
        row = await self.pg.fetchone(
            """
            SELECT order_id::text AS order_id,
                   strategy_id,
                   symbol,
                   order_ref,
                   note
            FROM core.core_order
            WHERE idempotency_key = $1
            """,
            idempotency_key,
        )
        resolved_order = {
            "order_id": str((row or {}).get("order_id") or order_id),
            "broker_order_id": str(order_id),
            "strategy_id": (row or {}).get("strategy_id"),
            "symbol": (row or {}).get("symbol"),
            "order_ref": (row or {}).get("order_ref"),
            "note": (row or {}).get("note"),
        }
        note_data = _merge_note_json(
            resolved_order.get("note"),
            {
                "flatten_lifecycle": {
                    "state": "submitted",
                    "submitted_at": datetime.utcnow().isoformat(),
                    "broker_order_id": str(order_id),
                    "close_only": bool(close_only),
                }
            },
        )
        note_obj = _load_note_obj(resolved_order.get("note"))
        lifecycle = note_obj.get("flatten_lifecycle") if isinstance(note_obj.get("flatten_lifecycle"), dict) else {}
        await self._record_flatten_transition(
            resolved_order=resolved_order,
            before_state=(lifecycle or {}).get("state") or FlattenLifecycleState.CREATED.value,
            after_state=FlattenLifecycleState.SUBMITTED.value,
            event_type="flatten_submitted",
            reason="flatten_action_submitted",
            payload={"broker_order_id": str(order_id), "close_only": bool(close_only)},
        )
        return note_data

    async def mark_rejected_by_idempotency(
        self,
        *,
        idempotency_key: str,
        reason: str,
        close_only: bool,
    ) -> str:
        row = await self.pg.fetchone(
            """
            SELECT order_id::text AS order_id,
                   strategy_id,
                   symbol,
                   order_ref,
                   note
            FROM core.core_order
            WHERE idempotency_key = $1
            """,
            idempotency_key,
        )
        resolved_order = {
            "order_id": (row or {}).get("order_id"),
            "strategy_id": (row or {}).get("strategy_id"),
            "symbol": (row or {}).get("symbol"),
            "order_ref": (row or {}).get("order_ref"),
            "note": (row or {}).get("note"),
        }
        note_data = _merge_note_json(
            resolved_order.get("note"),
            {
                "reject_reason": reason,
                "flatten_lifecycle": {
                    "state": "rejected",
                    "rejected_at": datetime.utcnow().isoformat(),
                    "reject_reason": reason,
                    "close_only": bool(close_only),
                },
            },
        )
        note_obj = _load_note_obj(resolved_order.get("note"))
        lifecycle = note_obj.get("flatten_lifecycle") if isinstance(note_obj.get("flatten_lifecycle"), dict) else {}
        await self._record_flatten_transition(
            resolved_order=resolved_order,
            before_state=(lifecycle or {}).get("state") or FlattenLifecycleState.CREATED.value,
            after_state=FlattenLifecycleState.REJECTED.value,
            event_type="flatten_rejected",
            reason=reason,
            payload={"close_only": bool(close_only)},
        )
        return note_data

    async def mark_order_lifecycle(
        self,
        *,
        order_uuid: Any,
        resolved_order: Dict[str, Any],
        state: str,
        payload: Optional[Dict[str, Any]] = None,
        note_updates: Optional[Dict[str, Any]] = None,
    ) -> None:
        if order_uuid is None or not resolved_order:
            return
        previous_note = _load_note_obj(resolved_order.get("note"))
        lifecycle = previous_note.get("flatten_lifecycle") if isinstance(previous_note.get("flatten_lifecycle"), dict) else {}
        previous_state = (lifecycle or {}).get("state") or _fallback_flatten_before_state(state).value
        note_data = _merge_note_json(
            resolved_order.get("note"),
            {
                **dict(note_updates or {}),
                "flatten_lifecycle": {"state": str(state or "").strip(), **dict(payload or {})},
            },
        )
        await self.pg.execute(
            """
            UPDATE core.core_order
            SET note = $1,
                updated_at = now()
            WHERE order_id = $2
            """,
            note_data,
            order_uuid,
        )
        await self._record_flatten_transition(
            resolved_order={**dict(resolved_order), "order_id": order_uuid, "note": resolved_order.get("note")},
            before_state=previous_state,
            after_state=state,
            event_type=f"flatten_{str(state or '').strip().lower()}",
            reason=str((payload or {}).get("confirmed_by") or (payload or {}).get("reject_reason") or state or "flatten_lifecycle_update"),
            payload=dict(payload or {}),
        )

    async def probe_completion(self, *, strategy_id: str, symbol: str) -> Dict[str, Any]:
        strategy_id = str(strategy_id or "").strip()
        symbol = str(symbol or "").strip().upper()
        if not strategy_id or not symbol:
            return {"state": "pending", "is_flat": False, "pos_qty": None, "open_order_count": None}
        truth = self.order_truth_service
        if truth is not None:
            try:
                pos_snapshot = await truth.effective_position(strategy_id=strategy_id, symbol=symbol)
                open_orders_snapshot = await truth.effective_open_orders(strategy_id=strategy_id)
                pos_qty = float(getattr(getattr(pos_snapshot, "position", None), "pos_qty", 0.0) or 0.0)
                open_count = sum(
                    1 for order in list(getattr(open_orders_snapshot, "orders", []) or [])
                    if str(getattr(order, "symbol", "") or "").strip().upper() == symbol
                )
                is_flat = abs(pos_qty) <= 1e-6 and open_count == 0
                return {
                    "state": "confirmed" if is_flat else "pending",
                    "is_flat": bool(is_flat),
                    "pos_qty": float(pos_qty),
                    "open_order_count": int(open_count),
                    "position_source": str(getattr(pos_snapshot, "source", "") or ""),
                    "open_orders_source": str(getattr(open_orders_snapshot, "source", "") or ""),
                }
            except Exception as exc:
                logger.warning(
                    "flatten_controller_probe_truth_failed",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
        pos_row = await self.pg.fetchone(
            """
            SELECT pos_qty
            FROM core.core_position
            WHERE strategy_id = $1 AND symbol = $2
            """,
            strategy_id,
            symbol,
        )
        pos_qty = float((pos_row or {}).get("pos_qty") or 0.0)
        try:
            open_count = int(
                await self.pg.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM core.core_order
                    WHERE strategy_id = $1
                      AND symbol = $2
                      AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                    """,
                    strategy_id,
                    symbol,
                )
                or 0
            )
        except Exception:
            _count_row = await self.pg.fetchone(
                """
                SELECT COUNT(*) AS count
                FROM core.core_order
                WHERE strategy_id = $1
                  AND symbol = $2
                  AND state IN ('NEW', 'SUBMITTED', 'PARTIAL')
                """,
                strategy_id,
                symbol,
            )
            open_count = int((_count_row or {}).get("count") or 0)
        is_flat = abs(pos_qty) <= 1e-6 and open_count == 0
        return {
            "state": "confirmed" if is_flat else "pending",
            "is_flat": bool(is_flat),
            "pos_qty": float(pos_qty),
            "open_order_count": int(open_count),
        }

    async def resolve_from_position(
        self,
        *,
        order_uuid: Any,
        resolved_order: Dict[str, Any],
        confirmed_by: str,
        fill_broker_order_id: Optional[Any] = None,
        unlock_reason: Optional[str] = None,
    ) -> str:
        strategy_id = str(resolved_order.get("strategy_id") or "").strip()
        symbol = str(resolved_order.get("symbol") or "").strip().upper()
        if not strategy_id or not symbol:
            return "pending"
        try:
            completion = await self.probe_completion(strategy_id=strategy_id, symbol=symbol)
        except Exception as exc:
            logger.warning(
                "flatten_controller_resolve_probe_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return "pending"
        is_flat = bool(completion.get("is_flat"))
        state = str(completion.get("state") or "pending")
        payload = {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_by": confirmed_by,
            "fill_broker_order_id": str(fill_broker_order_id) if fill_broker_order_id is not None else None,
        }
        await self.mark_order_lifecycle(
            order_uuid=order_uuid,
            resolved_order=resolved_order,
            state=state,
            payload=payload,
        )
        if is_flat and unlock_reason:
            await self.maybe_unlock_flatten_action_lock(strategy_id=strategy_id, symbol=symbol, reason=unlock_reason)
        return state

    async def reconcile_confirmed_if_flat(
        self,
        *,
        order_row: Dict[str, Any],
        ib_open_symbols: Set[str],
    ) -> bool:
        strategy_id = str(order_row.get("strategy_id") or "").strip()
        symbol = str(order_row.get("symbol") or "").strip().upper()
        if not strategy_id or not symbol or symbol in ib_open_symbols:
            return False
        truth = self.order_truth_service
        if truth is not None:
            try:
                completion = await self.probe_completion(strategy_id=strategy_id, symbol=symbol)
            except Exception:
                completion = {}
            if bool(completion.get("is_flat")):
                await self.mark_order_lifecycle(
                    order_uuid=order_row.get("order_id"),
                    resolved_order=order_row,
                    state="confirmed",
                    payload={
                        "confirmed_at": datetime.now(timezone.utc).isoformat(),
                        "confirmed_by": "runtime_reconciler",
                        "previous_state": str((((json.loads(order_row.get("note") or "{}")).get("flatten_lifecycle") or {}).get("state")) or ""),
                        "position_source": completion.get("position_source"),
                        "open_orders_source": completion.get("open_orders_source"),
                    },
                )
                return True
            return False
        try:
            pos_row = await self.pg.fetchone(
                """
                SELECT pos_qty
                FROM core.core_position
                WHERE strategy_id = $1 AND symbol = $2
                """,
                strategy_id,
                symbol,
            )
            pos_qty = float((pos_row or {}).get("pos_qty") or 0.0)
        except Exception:
            return False
        if abs(pos_qty) > 1e-6:
            return False
        await self.mark_order_lifecycle(
            order_uuid=order_row.get("order_id"),
            resolved_order=order_row,
            state="confirmed",
            payload={
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "confirmed_by": "runtime_reconciler",
                "previous_state": str((((json.loads(order_row.get("note") or "{}")).get("flatten_lifecycle") or {}).get("state")) or ""),
            },
        )
        return True

    async def maybe_unlock_flatten_action_lock(self, *, strategy_id: str, symbol: str, reason: str) -> bool:
        store = self.manual_intervention_lock_store
        if store is None:
            return False
        active_lock = await store.get_active(strategy_id=str(strategy_id).strip(), symbol=str(symbol).strip().upper())
        if not isinstance(active_lock, dict):
            return False
        if str(active_lock.get("reason") or "").strip().lower() != "flatten_action":
            return False
        return bool(await store.unlock(strategy_id=str(strategy_id).strip(), symbol=str(symbol).strip().upper()))
