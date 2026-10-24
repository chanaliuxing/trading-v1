from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

import structlog

logger = structlog.get_logger(__name__)


class FlattenLifecycleHandler:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def reconcile(self, *, db_orders: List[Dict[str, Any]], ib_open_orders: List[Any]) -> None:
        ib_open_symbols: Set[str] = set()
        for order in ib_open_orders or []:
            sym = str(getattr(order, "symbol", "") or "").strip().upper()
            if not sym:
                contract = getattr(order, "contract", None)
                if contract is not None:
                    sym = str(getattr(contract, "symbol", "") or "").strip().upper()
            if sym:
                ib_open_symbols.add(sym)

        for order in db_orders or []:
            note_raw = order.get("note")
            if not isinstance(note_raw, str) or not note_raw.strip():
                continue
            try:
                note = json.loads(note_raw)
            except Exception:
                continue
            if not isinstance(note, dict):
                continue
            lifecycle = note.get("flatten_lifecycle")
            if not isinstance(lifecycle, dict):
                continue
            state = str(lifecycle.get("state") or "").strip().lower()
            if state not in {"submitted", "pending"}:
                continue

            strategy_id = str(order.get("strategy_id") or "").strip()
            symbol = str(order.get("symbol") or "").strip().upper()
            if not strategy_id or not symbol:
                continue
            if symbol in ib_open_symbols:
                continue

            try:
                row = await self.runtime.pg.fetchone(
                    """
                    SELECT pos_qty
                    FROM core.core_position
                    WHERE strategy_id = $1 AND symbol = $2
                    """,
                    strategy_id,
                    symbol,
                )
                pos_qty = float((row or {}).get("pos_qty") or 0.0)
            except Exception:
                continue
            if abs(pos_qty) > 1e-6:
                continue

            try:
                changed = await self.runtime.flatten_controller.reconcile_confirmed_if_flat(
                    order_row=order,
                    ib_open_symbols=ib_open_symbols,
                )
            except Exception as exc:
                logger.warning(
                    "flatten_lifecycle_reconcile_update_failed",
                    order_id=order.get("order_id"),
                    strategy_id=strategy_id,
                    symbol=symbol,
                    error=str(exc),
                )
                continue
            if not changed:
                continue

            self.runtime._emit_audit(
                stage="flatten_auto_confirmed",
                severity="warning",
                strategy_id=strategy_id or None,
                order_id=str(order.get("order_id") or "") or None,
                broker_order_id=str(order.get("broker_order_id") or "") or None,
                perm_id=order.get("ib_perm_id"),
                symbol=symbol or None,
                status="auto_healed",
                reason="broker_flat_and_no_ib_open_orders",
                payload={
                    "previous_state": state,
                    "db_state": str(order.get("state") or ""),
                    "order_ref": str(order.get("order_ref") or ""),
                },
            )
            await self.maybe_unlock_flatten_action_lock(strategy_id=strategy_id, symbol=symbol)

    async def maybe_unlock_flatten_action_lock(self, *, strategy_id: str, symbol: str) -> None:
        store = self.runtime.manual_intervention_lock_store
        if store is None:
            return
        try:
            active_lock = await store.get_active(strategy_id=strategy_id, symbol=symbol)
        except Exception as exc:
            logger.warning(
                "reconciler_manual_lock_read_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return
        if not isinstance(active_lock, dict):
            return
        reason = str(active_lock.get("reason") or "").strip().lower()
        if reason != "flatten_action":
            return
        try:
            cleared = await store.unlock(strategy_id=strategy_id, symbol=symbol)
        except Exception as exc:
            logger.warning(
                "reconciler_flatten_lock_clear_failed",
                strategy_id=strategy_id,
                symbol=symbol,
                error=str(exc),
            )
            return
        if cleared:
            self.runtime._emit_audit(
                stage="flatten_action_lock_cleared",
                severity="warning",
                strategy_id=strategy_id or None,
                symbol=symbol or None,
                status="auto_healed",
                reason="flatten_confirmed_by_reconciler",
                payload={"lock_reason": reason},
            )

    async def audit(self, *, db_orders: List[Dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc)
        for order in db_orders or []:
            note_raw = order.get("note")
            if not isinstance(note_raw, str) or not note_raw.strip():
                continue
            try:
                note = json.loads(note_raw)
            except Exception:
                continue
            if not isinstance(note, dict):
                continue
            lifecycle = note.get("flatten_lifecycle")
            if not isinstance(lifecycle, dict):
                continue
            state = str(lifecycle.get("state") or "").strip().lower()
            if state not in {"submitted", "pending"}:
                continue
            updated_at = order.get("updated_at")
            if not isinstance(updated_at, datetime):
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age_sec = max(0.0, (now - updated_at.astimezone(timezone.utc)).total_seconds())
            if age_sec < float(self.runtime.flatten_incomplete_grace_seconds):
                continue
            symbol = str(order.get("symbol") or "")
            strategy_id = str(order.get("strategy_id") or "")
            pos_qty = None
            try:
                row = await self.runtime.pg.fetchone(
                    """
                    SELECT pos_qty
                    FROM core.core_position
                    WHERE strategy_id = $1 AND symbol = $2
                    """,
                    strategy_id,
                    symbol,
                )
                pos_qty = float((row or {}).get("pos_qty") or 0.0)
            except Exception:
                pos_qty = None
            self.runtime._emit_audit(
                stage="flatten_incomplete_detected",
                severity="warning",
                strategy_id=strategy_id or None,
                order_id=str(order.get("order_id") or "") or None,
                broker_order_id=str(order.get("broker_order_id") or "") or None,
                perm_id=order.get("ib_perm_id"),
                symbol=symbol or None,
                status="warning",
                reason="flatten_lifecycle_still_open",
                payload={
                    "flatten_state": state,
                    "age_sec": round(float(age_sec), 3),
                    "pos_qty": pos_qty,
                    "db_state": str(order.get("state") or ""),
                    "order_ref": str(order.get("order_ref") or ""),
                },
            )
