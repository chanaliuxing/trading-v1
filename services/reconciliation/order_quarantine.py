from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

from services.production.recovery_incident_service import RecoveryIncidentService
from services.production.order_safety_sanity import (
    is_safety_like_order,
    order_limit_px,
    order_stop_px,
    positive_float,
    validate_market_price_band,
)

logger = structlog.get_logger(__name__)

RUNTIME_QUARANTINE_INCIDENT_SOURCES = {
    "runtime_reconciler",
    "runtime_reconciler_orphan_detected",
    "runtime_reconciler_position_mismatch",
    "runtime_reconciler_strategy_recovery",
    "runtime_reconciler_terminal_orphan",
}


class OrderQuarantineHandler:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.recovery_incident_service: Optional[RecoveryIncidentService] = getattr(
            runtime, "recovery_incident_service", None
        )

    async def active_quarantine_summary(self) -> Dict[str, Any]:
        try:
            rows = await self.runtime.pg.fetchall(
                """
                SELECT
                    reason,
                    COUNT(*) AS active_count,
                    MAX(observed_at) AS last_observed_at
                FROM core.core_order_quarantine
                WHERE status = 'active'
                GROUP BY reason
                ORDER BY COUNT(*) DESC, reason ASC
                """
            )
        except Exception as exc:
            logger.warning("runtime_quarantine_summary_lookup_failed", error=str(exc))
            return {"active_count": 0, "by_reason": [], "lookup_failed": True}

        by_reason: List[Dict[str, Any]] = []
        total = 0
        for row in rows or []:
            payload = dict(row) if hasattr(row, "get") else {}
            count = int(payload.get("active_count") or 0)
            total += count
            by_reason.append(
                {
                    "reason": str(payload.get("reason") or "unknown"),
                    "active_count": count,
                    "last_observed_at": (
                        payload.get("last_observed_at").isoformat()
                        if isinstance(payload.get("last_observed_at"), datetime)
                        else payload.get("last_observed_at")
                    ),
                }
            )
        return {"active_count": total, "by_reason": by_reason, "lookup_failed": False}

    async def acknowledge_order_quarantine(
        self,
        *,
        actor: str,
        symbol: str,
        broker_order_id: Optional[str] = None,
        perm_id: Optional[int] = None,
        note: Optional[str] = None,
        resolve: bool = True,
    ) -> int:
        symbol_u = str(symbol or "").strip().upper()
        broker_order_id_v = str(broker_order_id or "").strip() or None
        perm_id_v = int(perm_id) if perm_id is not None else None
        if not symbol_u:
            return 0
        note_payload = {
            "manual_acknowledged": True,
            "ack_actor": str(actor or "").strip() or "unknown",
            "ack_note": str(note or "").strip() or None,
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        }
        status_target = "resolved" if resolve else "active"
        rows = await self.runtime.pg.execute(
            """
            UPDATE core.core_order_quarantine
            SET status = $1,
                resolved_at = CASE WHEN $1 = 'resolved' THEN now() ELSE resolved_at END,
                updated_at = now(),
                note = $2
            WHERE symbol = $3
              AND status = 'active'
              AND ($4::text IS NULL OR broker_order_id = $4)
              AND ($5::bigint IS NULL OR ib_perm_id = $5 OR perm_id = $5)
            """,
            status_target,
            json.dumps(note_payload, ensure_ascii=False),
            symbol_u,
            broker_order_id_v,
            perm_id_v,
        )
        return self._rows_affected(rows)

    async def quarantine_detected_orphans(self, orphans: List[Any]) -> List[Any]:
        unresolved: List[Any] = []
        for orphan in orphans or []:
            try:
                symbol = str(getattr(orphan, "symbol", "") or "").strip().upper()
                broker_order_id = str(getattr(orphan, "order_id", "") or "").strip()
                order_ref = str(getattr(orphan, "order_ref", "") or "").strip()
                perm_id = self.runtime._normalize_perm_id(getattr(orphan, "perm_id", None))
                ib_client_id_raw = getattr(orphan, "client_id", None)
                if ib_client_id_raw is None:
                    ib_client_id_raw = getattr(orphan, "clientId", None)
                if ib_client_id_raw is None:
                    order_obj = getattr(orphan, "order", None)
                    ib_client_id_raw = getattr(order_obj, "clientId", None) if order_obj is not None else None
                ib_client_id = int(ib_client_id_raw) if ib_client_id_raw is not None else None
                side = str(getattr(orphan, "side", "") or "").strip().upper()
                status = str(getattr(orphan, "status", "") or "").strip()
                order_type = str(getattr(orphan, "order_type", "") or "").strip()
                qty_raw = getattr(orphan, "qty", None)
                qty = float(qty_raw) if qty_raw is not None else None
                if await self._sync_known_orphan_identity(
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                    order_ref=order_ref,
                    status=status,
                    side=side,
                    qty=qty,
                    order_type=order_type,
                    limit_px=order_limit_px(orphan),
                    stop_px=order_stop_px(orphan),
                ):
                    continue
                already_quarantined = await self.has_active_order_quarantine(
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                )
                await self.upsert_order_quarantine(
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    perm_id=perm_id,
                    ib_client_id=ib_client_id,
                    order_ref=order_ref,
                    side=side or None,
                    qty=qty,
                    order_type=order_type or None,
                    source="runtime_reconciler_orphan_detected",
                    reason="unknown_broker_order_detected",
                    payload={"status": status},
                )
                if not already_quarantined:
                    unresolved.append(orphan)
            except Exception as exc:
                logger.warning("runtime_orphan_quarantine_failed", error=str(exc))
                unresolved.append(orphan)
        return unresolved

    async def _sync_known_orphan_identity(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        perm_id: Optional[str],
        order_ref: str,
        status: str,
        side: str = "",
        qty: Optional[float] = None,
        order_type: str = "",
        limit_px: Optional[float] = None,
        stop_px: Optional[float] = None,
    ) -> bool:
        order_ref_v = str(order_ref or "").strip()
        if not order_ref_v:
            return False
        try:
            row = await self.runtime.pg.fetchone(
                """
                SELECT order_id, strategy_id, symbol, side, qty, order_type, limit_px, stop_px, state
                FROM core.core_order
                WHERE order_ref = $1
                   OR COALESCE(note, '') LIKE ('%' || $1 || '%')
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                order_ref_v,
            )
        except Exception as exc:
            logger.warning("runtime_orphan_identity_lookup_failed", order_ref=order_ref_v, error=str(exc))
            return False
        if not row:
            return await self._sync_known_protection_orphan_identity(
                symbol=symbol,
                broker_order_id=broker_order_id,
                perm_id=perm_id,
                order_ref=order_ref_v,
                status=status,
                side=side,
                qty=qty,
                order_type=order_type,
                limit_px=limit_px,
                stop_px=stop_px,
            )
        state = str(row.get("state") or "").strip().upper()
        if state in {"FILLED", "CANCELLED", "REJECTED"}:
            return False
        order_like = {
            "order_ref": order_ref_v,
            "symbol": str(symbol or row.get("symbol") or "").strip().upper(),
            "side": str(side or row.get("side") or "").strip().upper(),
            "qty": qty if qty is not None else row.get("qty"),
            "order_type": str(order_type or row.get("order_type") or "").strip().upper(),
            "limit_px": limit_px if limit_px is not None else row.get("limit_px"),
            "stop_px": stop_px if stop_px is not None else row.get("stop_px"),
        }
        if is_safety_like_order(order_like):
            if "UNKNOWN_ACCOUNT" in order_ref_v.upper():
                self.runtime._emit_audit(
                    stage="orphan_identity_sync_rejected",
                    severity="error",
                    strategy_id=str(row.get("strategy_id") or ""),
                    order_id=str(row.get("order_id") or ""),
                    broker_order_id=str(broker_order_id or "").strip() or None,
                    symbol=str(symbol or "").strip().upper() or None,
                    status="rejected",
                    reason="unknown_account_safety_order_ref",
                    payload={"order_ref": order_ref_v, "ib_status": str(status or "")},
                )
                return False
            market_ref = await self._market_ref_px(str(symbol or row.get("symbol") or "").strip().upper())
            ok, reason, details = validate_market_price_band(
                order=order_like,
                market_ref_px=market_ref,
                require_ref_for_priced_order=True,
            )
            if not ok:
                self.runtime._emit_audit(
                    stage="orphan_identity_sync_rejected",
                    severity="error",
                    strategy_id=str(row.get("strategy_id") or ""),
                    order_id=str(row.get("order_id") or ""),
                    broker_order_id=str(broker_order_id or "").strip() or None,
                    symbol=str(symbol or "").strip().upper() or None,
                    status="rejected",
                    reason=reason,
                    payload={
                        "order_ref": order_ref_v,
                        "ib_status": str(status or ""),
                        "market_ref_px": market_ref,
                        **details,
                    },
                )
                return False
        try:
            perm_value = int(perm_id) if perm_id is not None else None
        except Exception:
            perm_value = None
        try:
            mapped_state = self.runtime._map_ib_status_to_db_state(str(status or ""))
        except Exception:
            mapped_state = "SUBMITTED"
        await self.runtime.pg.execute(
            """
            UPDATE core.core_order
            SET broker_order_id = COALESCE(NULLIF($1, ''), broker_order_id),
                ib_perm_id = COALESCE($2, ib_perm_id),
                state = CASE WHEN state IN ('NEW', 'SUBMITTED', 'PARTIAL') THEN $3 ELSE state END,
                updated_at = now()
            WHERE order_id = $4
            """,
            str(broker_order_id or "").strip(),
            perm_value,
            mapped_state,
            row.get("order_id"),
        )
        self.runtime._emit_audit(
            stage="orphan_identity_synced",
            severity="info",
            strategy_id=str(row.get("strategy_id") or ""),
            order_id=str(row.get("order_id") or ""),
            broker_order_id=str(broker_order_id or "").strip() or None,
            perm_id=perm_value,
            symbol=str(symbol or "").strip().upper() or None,
            status="synced",
            reason="known_order_ref_orphan_converged",
            payload={"order_ref": order_ref_v, "ib_status": str(status or ""), "db_state_before": state},
        )
        return True

    async def _sync_known_protection_orphan_identity(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        perm_id: Optional[str],
        order_ref: str,
        status: str,
        side: str = "",
        qty: Optional[float] = None,
        order_type: str = "",
        limit_px: Optional[float] = None,
        stop_px: Optional[float] = None,
    ) -> bool:
        order_ref_v = str(order_ref or "").strip()
        if not order_ref_v:
            return False
        try:
            perm_value = int(perm_id) if perm_id is not None else None
        except Exception:
            perm_value = None
        try:
            row = await self.runtime.pg.fetchone(
                """
                SELECT protection_key, account_id, strategy_id, strategy_instance_id,
                       symbol, lot_id, side, desired_qty, desired_price,
                       broker_order_id, ib_perm_id, order_ref, state, order_role,
                       protection_kind, position_owner_key
                FROM core.core_protection_order_registry
                WHERE order_ref = $1
                   OR (NULLIF($2, '') IS NOT NULL AND broker_order_id = NULLIF($2, '')::bigint)
                   OR ($3::bigint IS NOT NULL AND ib_perm_id = $3::bigint)
                ORDER BY updated_at DESC NULLS LAST, first_seen_at DESC NULLS LAST
                LIMIT 1
                """,
                order_ref_v,
                str(broker_order_id or "").strip(),
                perm_value,
            )
        except Exception as exc:
            logger.warning("runtime_orphan_protection_identity_lookup_failed", order_ref=order_ref_v, error=str(exc))
            return False
        if not row:
            return False

        state = str(row.get("state") or "").strip().upper()
        if state in {"FILLED", "CANCELLED", "REJECTED", "INACTIVE"}:
            return False

        account_id = str(row.get("account_id") or "").strip()
        strategy_instance_id = str(row.get("strategy_instance_id") or "").strip()
        row_symbol = str(row.get("symbol") or "").strip().upper()
        lot_id = str(row.get("lot_id") or "").strip()
        if (
            not account_id
            or account_id.upper() == "UNKNOWN_ACCOUNT"
            or not strategy_instance_id
            or not row_symbol
            or not lot_id
        ):
            self.runtime._emit_audit(
                stage="orphan_protection_identity_sync_rejected",
                severity="error",
                strategy_id=str(row.get("strategy_id") or ""),
                broker_order_id=str(broker_order_id or "").strip() or None,
                perm_id=perm_value,
                symbol=str(symbol or row_symbol or "").strip().upper() or None,
                status="rejected",
                reason="incomplete_protection_owner_identity",
                payload={
                    "order_ref": order_ref_v,
                    "account_id_present": bool(account_id),
                    "strategy_instance_id_present": bool(strategy_instance_id),
                    "lot_id_present": bool(lot_id),
                },
            )
            return False
        symbol_u = str(symbol or "").strip().upper()
        if symbol_u and row_symbol and symbol_u != row_symbol:
            self.runtime._emit_audit(
                stage="orphan_protection_identity_sync_rejected",
                severity="error",
                strategy_id=str(row.get("strategy_id") or ""),
                broker_order_id=str(broker_order_id or "").strip() or None,
                perm_id=perm_value,
                symbol=symbol_u,
                status="rejected",
                reason="protection_owner_symbol_mismatch",
                payload={"order_ref": order_ref_v, "registry_symbol": row_symbol},
            )
            return False

        order_like = {
            "order_ref": order_ref_v,
            "symbol": symbol_u or row_symbol,
            "side": str(side or row.get("side") or "").strip().upper(),
            "qty": qty if qty is not None else row.get("desired_qty"),
            "order_type": str(order_type or row.get("order_role") or row.get("protection_kind") or "").strip().upper(),
            "limit_px": limit_px if limit_px is not None else row.get("desired_price"),
            "stop_px": stop_px if stop_px is not None else row.get("desired_price"),
        }
        if not is_safety_like_order(order_like):
            return False
        if "UNKNOWN_ACCOUNT" in order_ref_v.upper():
            self.runtime._emit_audit(
                stage="orphan_protection_identity_sync_rejected",
                severity="error",
                strategy_id=str(row.get("strategy_id") or ""),
                broker_order_id=str(broker_order_id or "").strip() or None,
                perm_id=perm_value,
                symbol=symbol_u or row_symbol or None,
                status="rejected",
                reason="unknown_account_safety_order_ref",
                payload={"order_ref": order_ref_v, "ib_status": str(status or "")},
            )
            return False
        market_ref = await self._market_ref_px(symbol_u or row_symbol)
        ok, reason, details = validate_market_price_band(
            order=order_like,
            market_ref_px=market_ref,
            require_ref_for_priced_order=True,
        )
        if not ok:
            self.runtime._emit_audit(
                stage="orphan_protection_identity_sync_rejected",
                severity="error",
                strategy_id=str(row.get("strategy_id") or ""),
                broker_order_id=str(broker_order_id or "").strip() or None,
                perm_id=perm_value,
                symbol=symbol_u or row_symbol or None,
                status="rejected",
                reason=reason,
                payload={
                    "order_ref": order_ref_v,
                    "ib_status": str(status or ""),
                    "market_ref_px": market_ref,
                    **details,
                },
            )
            return False
        try:
            mapped_state = self.runtime._map_ib_status_to_db_state(str(status or ""))
        except Exception:
            mapped_state = "SUBMITTED"
        await self.runtime.pg.execute(
            """
            UPDATE core.core_protection_order_registry
            SET broker_order_id = COALESCE(NULLIF($1, '')::bigint, broker_order_id),
                ib_perm_id = COALESCE($2::bigint, ib_perm_id),
                state = CASE WHEN state IN ('NEW', 'SUBMITTED', 'PARTIAL', 'ACTIVE') THEN $3 ELSE state END,
                updated_at = now()
            WHERE protection_key = $4
            """,
            str(broker_order_id or "").strip(),
            perm_value,
            mapped_state,
            row.get("protection_key"),
        )
        self.runtime._emit_audit(
            stage="orphan_protection_identity_synced",
            severity="info",
            strategy_id=str(row.get("strategy_id") or ""),
            broker_order_id=str(broker_order_id or "").strip() or None,
            perm_id=perm_value,
            symbol=symbol_u or row_symbol or None,
            status="synced",
            reason="known_protection_registry_orphan_converged",
            payload={
                "order_ref": order_ref_v,
                "ib_status": str(status or ""),
                "db_state_before": state,
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "lot_id": lot_id,
                "position_owner_key": str(row.get("position_owner_key") or ""),
            },
        )
        return True

    async def _market_ref_px(self, symbol: str) -> Optional[float]:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return None
        adapter = getattr(self.runtime, "adapter", None)
        if adapter is None:
            adapter = getattr(self.runtime, "ib_adapter", None)
        if adapter is None:
            return None
        try:
            positions_fn = getattr(adapter, "positions", None)
            if callable(positions_fn):
                raw_positions = positions_fn()
                if hasattr(raw_positions, "__await__"):
                    raw_positions = await raw_positions
                for item in raw_positions or []:
                    contract = getattr(item, "contract", None)
                    item_symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
                    if item_symbol != symbol_u:
                        continue
                    return (
                        positive_float(getattr(item, "marketPrice", None))
                        or positive_float(getattr(item, "market_price", None))
                        or positive_float(getattr(item, "last", None))
                        or positive_float(getattr(item, "avgCost", None))
                        or positive_float(getattr(item, "averageCost", None))
                    )
        except Exception as exc:
            logger.warning("runtime_orphan_market_ref_lookup_failed", symbol=symbol_u, error=str(exc))
        return None

    async def resolve_cleared_order_quarantines(
        self,
        *,
        db_by_broker_id: Dict[Any, Dict[str, Any]],
        db_by_perm_id: Dict[str, Dict[str, Any]],
        ib_by_id: Dict[Any, Any],
        ib_by_perm_id: Dict[str, Any],
    ) -> int:
        try:
            rows = await self.runtime.pg.fetchall(
                """
                SELECT symbol, broker_order_id, perm_id, ib_perm_id, reason, status, note
                FROM core.core_order_quarantine
                WHERE status = 'active'
                  AND reason = 'unknown_broker_order_detected'
                """
            )
        except Exception as exc:
            logger.warning("runtime_quarantine_resolution_lookup_failed", error=str(exc))
            return 0

        resolved = 0
        for row in rows or []:
            symbol = str(row.get("symbol") or "").strip().upper()
            broker_norm = self.runtime._normalize_broker_order_id(row.get("broker_order_id"))
            perm_norm = self.runtime._normalize_perm_id(row.get("ib_perm_id") or row.get("perm_id"))
            present_in_ib = (
                (broker_norm is not None and broker_norm in ib_by_id)
                or (perm_norm is not None and perm_norm in ib_by_perm_id)
            )
            present_in_db = (
                (broker_norm is not None and broker_norm in db_by_broker_id)
                or (perm_norm is not None and perm_norm in db_by_perm_id)
            )
            if present_in_ib and present_in_db:
                updated = await self.acknowledge_order_quarantine(
                    actor="runtime_reconciler",
                    symbol=symbol,
                    broker_order_id=str(row.get("broker_order_id") or "").strip() or None,
                    perm_id=int(perm_norm) if perm_norm is not None else None,
                    note="auto_resolved_after_broker_and_db_converged",
                    resolve=True,
                )
                if updated > 0:
                    resolved += updated
                    self._resolve_recovery_incident_for_quarantine(
                        symbol=symbol,
                        source=self._note_source(row.get("note")) or "runtime_reconciler_orphan_detected",
                    )
                    self.runtime._emit_audit(
                        stage="order_quarantine_auto_resolved",
                        severity="info",
                        symbol=symbol or None,
                        broker_order_id=str(row.get("broker_order_id") or "").strip() or None,
                        perm_id=int(perm_norm) if perm_norm is not None else None,
                        status="resolved",
                        reason="broker_and_db_converged",
                    )
                continue
            if present_in_ib or present_in_db:
                continue
            updated = await self.acknowledge_order_quarantine(
                actor="runtime_reconciler",
                symbol=symbol,
                broker_order_id=str(row.get("broker_order_id") or "").strip() or None,
                perm_id=int(perm_norm) if perm_norm is not None else None,
                note="auto_resolved_after_broker_and_db_cleared",
                resolve=True,
            )
            if updated > 0:
                resolved += updated
                self._resolve_recovery_incident_for_quarantine(
                    symbol=symbol,
                    source=self._note_source(row.get("note")) or "runtime_reconciler_orphan_detected",
                )
                self.runtime._emit_audit(
                    stage="order_quarantine_auto_resolved",
                    severity="info",
                    symbol=symbol or None,
                    broker_order_id=str(row.get("broker_order_id") or "").strip() or None,
                    perm_id=int(perm_norm) if perm_norm is not None else None,
                    status="resolved",
                    reason="cleared_from_broker_and_db",
                )
        return resolved

    async def resolve_converged_synthetic_quarantines(
        self,
        *,
        db_orders: List[Dict[str, Any]],
        ib_orders: List[Any],
        ghosts: List[Dict[str, Any]],
        orphans: List[Any],
        mismatches: List[Any],
    ) -> int:
        if ghosts or orphans or mismatches:
            return 0
        db_symbols = {
            str((order or {}).get("symbol") or "").strip().upper()
            for order in db_orders or []
            if str((order or {}).get("symbol") or "").strip()
        }
        ib_symbols = {
            self._ib_order_symbol(order)
            for order in ib_orders or []
            if self._ib_order_symbol(order)
        }
        converged_symbols = db_symbols & ib_symbols
        if not converged_symbols:
            return 0
        try:
            rows = await self.runtime.pg.fetchall(
                """
                SELECT symbol, reason, note
                FROM core.core_order_quarantine
                WHERE status = 'active'
                  AND reason IN ('db_persist_submitted_failed', 'recovery_action_not_executable')
                ORDER BY symbol, reason
                """
            )
        except Exception as exc:
            logger.warning("runtime_synthetic_quarantine_resolution_lookup_failed", error=str(exc))
            return 0

        resolved = 0
        for row in rows or []:
            symbol = str(row.get("symbol") or "").strip().upper()
            reason = str(row.get("reason") or "").strip()
            if symbol not in converged_symbols:
                continue
            updated = await self.acknowledge_order_quarantine(
                actor="runtime_reconciler",
                symbol=symbol,
                note=f"auto_resolved_synthetic_quarantine_after_order_truth_converged:{reason}",
                resolve=True,
            )
            if updated > 0:
                resolved += updated
                self._resolve_recovery_incident_for_quarantine(
                    symbol=symbol,
                    source=self._note_source(row.get("note")) or "runtime_reconciler",
                )
                self.runtime._emit_audit(
                    stage="synthetic_order_quarantine_auto_resolved",
                    severity="info",
                    symbol=symbol,
                    status="resolved",
                    reason="order_truth_converged",
                    payload={"source_reason": reason, "updated_count": updated},
                )
        return resolved

    async def resolve_cleared_position_quarantines(
        self,
        *,
        position_metadata: Dict[str, Any],
        db_orders: List[Dict[str, Any]],
        ib_orders: List[Any],
    ) -> int:
        ib_positions = {
            str(symbol or "").strip().upper(): float(qty or 0.0)
            for symbol, qty in dict(position_metadata.get("ib_positions") or {}).items()
        }
        ledger_positions = {
            str(symbol or "").strip().upper(): float(qty or 0.0)
            for symbol, qty in dict(position_metadata.get("ledger_positions") or {}).items()
        }
        db_open_symbols = {
            str((order or {}).get("symbol") or "").strip().upper()
            for order in db_orders or []
            if str((order or {}).get("symbol") or "").strip()
        }
        ib_open_symbols = {
            self._ib_order_symbol(order)
            for order in ib_orders or []
            if self._ib_order_symbol(order)
        }
        try:
            rows = await self.runtime.pg.fetchall(
                """
                SELECT symbol, reason, COUNT(*) AS active_count
                FROM core.core_order_quarantine
                WHERE status = 'active'
                  AND reason IN (
                    'broker_only_position_requires_manual_intervention',
                    'recovery_action_not_executable',
                    'reglite_block',
                    'recovery_action_submit_exception',
                    'db_persist_submitted_failed'
                  )
                GROUP BY symbol, reason
                ORDER BY symbol, reason
                """
            )
        except Exception as exc:
            logger.warning("runtime_position_quarantine_resolution_lookup_failed", error=str(exc))
            return 0

        resolved = 0
        for row in rows or []:
            symbol = str(row.get("symbol") or "").strip().upper()
            reason = str(row.get("reason") or "").strip()
            if not symbol:
                continue
            if abs(float(ib_positions.get(symbol, 0.0) or 0.0)) > 1e-6:
                continue
            if abs(float(ledger_positions.get(symbol, 0.0) or 0.0)) > 1e-6:
                continue
            if symbol in db_open_symbols or symbol in ib_open_symbols:
                continue
            updated = await self.acknowledge_order_quarantine(
                actor="runtime_reconciler",
                symbol=symbol,
                note=f"auto_resolved_position_quarantine_after_flat_truth:{reason}",
                resolve=True,
            )
            if updated > 0:
                resolved += updated
                self._resolve_recovery_incident_for_quarantine(symbol=symbol, source=None)
                self.runtime._emit_audit(
                    stage="position_quarantine_auto_resolved",
                    severity="info",
                    symbol=symbol,
                    status="resolved",
                    reason="broker_ledger_and_orders_flat",
                    payload={"source_reason": reason, "updated_count": updated},
                )
        return resolved

    @staticmethod
    def _ib_order_symbol(order: Any) -> str:
        if isinstance(order, dict):
            return str(order.get("symbol") or "").strip().upper()
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        if symbol:
            return symbol
        contract = getattr(order, "contract", None)
        if contract is not None:
            return str(getattr(contract, "symbol", "") or "").strip().upper()
        return ""

    async def has_active_order_quarantine(
        self,
        *,
        broker_order_id: str,
        perm_id: Optional[int],
    ) -> bool:
        try:
            row = await self.runtime.pg.fetchone(
                """
                SELECT TRUE AS active_quarantine
                FROM core.core_order_quarantine
                WHERE status = 'active'
                  AND (
                        (NULLIF($1, '') IS NOT NULL AND broker_order_id = NULLIF($1, ''))
                     OR ($2::bigint IS NOT NULL AND ib_perm_id = $2::bigint)
                  )
                LIMIT 1
                """,
                str(broker_order_id or "").strip(),
                int(perm_id) if perm_id is not None else None,
            )
            if row is None:
                return False
            if hasattr(row, "get"):
                return bool(row.get("active_quarantine") is True)
            return True
        except Exception:
            return False

    async def upsert_order_quarantine(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        perm_id: Optional[int],
        ib_client_id: Optional[int] = None,
        order_ref: str,
        side: Optional[str],
        qty: Optional[float],
        order_type: Optional[str],
        source: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        note_payload: Dict[str, Any] = dict(payload or {})
        note_payload.update(
            {
                "source": str(source or "").strip() or "runtime_reconciler",
                "reason": str(reason or "").strip() or "unknown",
                "broker_order_id": str(broker_order_id or "").strip() or None,
                "perm_id": int(perm_id) if perm_id is not None else None,
                "ib_client_id": int(ib_client_id) if ib_client_id is not None else None,
                "order_ref": str(order_ref or "").strip() or None,
                "side": str(side or "").strip().upper() or None,
                "qty": float(qty) if qty is not None else None,
                "order_type": str(order_type or "").strip() or None,
            }
        )
        note_json = json.dumps(note_payload, ensure_ascii=False)
        try:
            broker_order_id_text = str(broker_order_id or "").strip()
            perm_value = int(perm_id) if perm_id is not None else None
            client_value = int(ib_client_id) if ib_client_id is not None else None
            order_ref_text = str(order_ref or "")
            reason_text = str(reason or "unknown")
            if perm_value is not None:
                await self.runtime.pg.execute(
                    """
                    INSERT INTO core.core_order_quarantine (
                        symbol,
                        broker_order_id,
                        perm_id,
                        ib_perm_id,
                        ib_client_id,
                        order_ref,
                        reason,
                        status,
                        note,
                        observed_at,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, NULLIF($2, ''), $3, $3, $4, NULLIF($5, ''), $6, 'active', $7, now(), now(), now())
                    ON CONFLICT (ib_perm_id)
                    WHERE status = 'active' AND ib_perm_id IS NOT NULL
                    DO UPDATE
                    SET symbol = EXCLUDED.symbol,
                        broker_order_id = EXCLUDED.broker_order_id,
                        perm_id = EXCLUDED.perm_id,
                        ib_client_id = COALESCE(EXCLUDED.ib_client_id, core.core_order_quarantine.ib_client_id),
                        order_ref = EXCLUDED.order_ref,
                        reason = EXCLUDED.reason,
                        note = EXCLUDED.note,
                        observed_at = now(),
                        updated_at = now()
                    """,
                    sym,
                    broker_order_id_text,
                    perm_value,
                    client_value,
                    order_ref_text,
                    reason_text,
                    note_json,
                )
            elif broker_order_id_text:
                await self.runtime.pg.execute(
                    """
                    INSERT INTO core.core_order_quarantine (
                        symbol,
                        broker_order_id,
                        perm_id,
                        ib_perm_id,
                        ib_client_id,
                        order_ref,
                        reason,
                        status,
                        note,
                        observed_at,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, NULLIF($2, ''), NULL, NULL, $3, NULLIF($4, ''), $5, 'active', $6, now(), now(), now())
                    ON CONFLICT (broker_order_id)
                    WHERE status = 'active' AND broker_order_id IS NOT NULL
                    DO UPDATE
                    SET symbol = EXCLUDED.symbol,
                        ib_client_id = COALESCE(EXCLUDED.ib_client_id, core.core_order_quarantine.ib_client_id),
                        order_ref = EXCLUDED.order_ref,
                        reason = EXCLUDED.reason,
                        note = EXCLUDED.note,
                        observed_at = now(),
                        updated_at = now()
                    """,
                    sym,
                    broker_order_id_text,
                    client_value,
                    order_ref_text,
                    reason_text,
                    note_json,
                )
            else:
                updated = await self.runtime.pg.execute(
                    """
                    UPDATE core.core_order_quarantine
                    SET note = $6,
                        observed_at = now(),
                        updated_at = now()
                    WHERE status = 'active'
                      AND symbol = $1
                      AND broker_order_id IS NULL
                      AND ib_perm_id IS NULL
                      AND COALESCE(order_ref, '') = COALESCE(NULLIF($2, ''), '')
                      AND reason = $3
                      AND COALESCE((note::jsonb)->>'source', '') = $4
                      AND COALESCE((note::jsonb)->>'strategy_id', '') = $5
                    """,
                    sym,
                    order_ref_text,
                    reason_text,
                    str(note_payload.get("source") or ""),
                    str(note_payload.get("strategy_id") or ""),
                    note_json,
                )
                if self._rows_affected(updated) > 0:
                    self._upsert_recovery_incident_for_quarantine(
                        symbol=sym,
                        qty=qty,
                        reason=reason_text,
                        payload=note_payload,
                    )
                    return
                await self.runtime.pg.execute(
                    """
                    INSERT INTO core.core_order_quarantine (
                        symbol,
                        broker_order_id,
                        perm_id,
                        ib_perm_id,
                        ib_client_id,
                        order_ref,
                        reason,
                        status,
                        note,
                        observed_at,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, NULL, NULL, NULL, $2, NULLIF($3, ''), $4, 'active', $5, now(), now(), now())
                    """,
                    sym,
                    client_value,
                    order_ref_text,
                    reason_text,
                    note_json,
                )
        except Exception as exc:
            logger.warning(
                "runtime_reconciler_order_quarantine_upsert_failed",
                symbol=sym,
                broker_order_id=str(broker_order_id or ""),
                reason=str(reason or "unknown"),
                error=str(exc),
            )
            return
        self._upsert_recovery_incident_for_quarantine(
            symbol=sym,
            qty=qty,
            reason=reason_text,
            payload=note_payload,
        )
        emit_audit = getattr(self.runtime, "_emit_audit", None)
        if callable(emit_audit):
            emit_audit(
                stage="order_quarantine_upserted",
                severity="warning",
                symbol=sym,
                broker_order_id=str(broker_order_id or "").strip() or None,
                perm_id=int(perm_id) if perm_id is not None else None,
                status="active",
                reason=reason_text,
                payload={
                    "source": str(source or "").strip() or "runtime_reconciler",
                    "order_ref": order_ref_text or None,
                    "ib_client_id": client_value,
                },
            )

    @staticmethod
    def _rows_affected(status: Any) -> int:
        try:
            text = str(status or "").strip()
            return int(text.rsplit(" ", 1)[-1])
        except Exception:
            return 0

    def _upsert_recovery_incident_for_quarantine(
        self,
        *,
        symbol: str,
        qty: Optional[float],
        reason: str,
        payload: Dict[str, Any],
    ) -> None:
        service = self.recovery_incident_service
        if service is None:
            return
        strategy_id = str(payload.get("strategy_id") or "").strip()
        if not strategy_id:
            strategy_ids = payload.get("strategy_ids")
            if isinstance(strategy_ids, list) and strategy_ids:
                strategy_id = str(strategy_ids[0] or "").strip()
        if not strategy_id:
            strategy_id = "runtime_reconciler"
        qty_value = qty
        if qty_value is None:
            qty_value = payload.get("target_qty")
        try:
            qty_f = abs(float(qty_value or 0.0))
        except Exception:
            qty_f = 0.0
        source = str(payload.get("source") or "").strip() or "runtime_reconciler"
        now_et = self._now_et()
        try:
            service.upsert_active(
                strategy_id=strategy_id,
                symbol=symbol,
                qty=qty_f,
                reason=str(reason or "unknown"),
                now_et=now_et,
                source=source,
                active_quarantine=True,
                protected_or_flattening=False,
            )
            service.write_index(now_et=now_et)
        except Exception as exc:
            logger.warning(
                "runtime_reconciler_recovery_incident_upsert_failed",
                symbol=str(symbol or "").strip().upper(),
                reason=str(reason or "unknown"),
                error=str(exc),
            )

    def _resolve_recovery_incident_for_quarantine(self, *, symbol: str, source: Optional[str]) -> None:
        service = self.recovery_incident_service
        if service is None:
            return
        now_et = self._now_et()
        try:
            if source:
                resolved = service.resolve_symbol_verified_flat(symbol=symbol, source=source, now_et=now_et)
            else:
                resolved = []
                for item in sorted(RUNTIME_QUARANTINE_INCIDENT_SOURCES):
                    resolved.extend(service.resolve_symbol_verified_flat(symbol=symbol, source=item, now_et=now_et))
            service.write_index(now_et=now_et, resolved_verified_flat_count=len(resolved))
        except Exception as exc:
            logger.warning(
                "runtime_reconciler_recovery_incident_resolve_failed",
                symbol=str(symbol or "").strip().upper(),
                source=str(source or ""),
                error=str(exc),
            )

    def _now_et(self) -> datetime:
        tz = getattr(self.runtime, "_et_tz", timezone.utc)
        return datetime.now(tz)

    @staticmethod
    def _note_source(raw_note: Any) -> Optional[str]:
        if isinstance(raw_note, dict):
            return str(raw_note.get("source") or "").strip() or None
        if isinstance(raw_note, str) and raw_note.strip():
            try:
                parsed = json.loads(raw_note)
            except Exception:
                return None
            if isinstance(parsed, dict):
                return str(parsed.get("source") or "").strip() or None
        return None
