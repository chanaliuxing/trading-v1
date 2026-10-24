from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import structlog

from libs.options.internal_symbol import is_internal_opt_symbol

logger = structlog.get_logger(__name__)


class PositionSyncHandler:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def audit_positions(
        self,
        *,
        db_open_orders: Optional[List[Dict[str, Any]]] = None,
        ib_open_orders: Optional[List[Any]] = None,
    ) -> None:
        try:
            broker_truth_connected = self.runtime._broker_truth_connected()
            if hasattr(self.runtime.adapter, "positions"):
                ib_positions = await self.runtime.adapter.positions()
            elif hasattr(self.runtime.adapter, "ib"):
                ib_positions = self.runtime.adapter.ib.positions()
            else:
                return

            ib_map: Dict[str, float] = {}
            for p in ib_positions or []:
                try:
                    sym = self.runtime._ib_position_symbol(p)
                    if not sym:
                        continue
                    qty = float(getattr(p, "position", 0.0) or 0.0)
                    if abs(qty) <= 0.0:
                        continue
                    ib_map[sym] = float(ib_map.get(sym, 0.0) + qty)
                except Exception:
                    continue

            rows = await self.runtime.pg.fetchall(
                """
                SELECT strategy_id, symbol, pos_qty
                FROM core.core_position
                WHERE abs(pos_qty) > 0.000001
                """
            )
            db_rows_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
            db_map: Dict[str, float] = {}
            for row in rows or []:
                sym = str((row or {}).get("symbol") or "").strip().upper()
                if not sym:
                    continue
                qty = float((row or {}).get("pos_qty", 0.0) or 0.0)
                if abs(qty) <= 0.0:
                    continue
                db_rows_by_symbol.setdefault(sym, []).append(row)
                db_map[sym] = float(db_map.get(sym, 0.0) + qty)

            db_open_symbols: Set[str] = set()
            for o in db_open_orders or []:
                sym = str((o or {}).get("symbol") or "").strip().upper()
                if sym:
                    db_open_symbols.add(sym)

            ib_open_symbols: Set[str] = set()
            for o in ib_open_orders or []:
                sym = str(getattr(o, "symbol", "") or "").strip().upper()
                if not sym:
                    contract = getattr(o, "contract", None)
                    if contract is not None:
                        sym = str(getattr(contract, "symbol", "") or "").strip().upper()
                if sym:
                    ib_open_symbols.add(sym)

            for sym in set(ib_map.keys()) | set(db_map.keys()):
                i_qty = ib_map.get(sym, 0.0)
                d_qty = db_map.get(sym, 0.0)
                if abs(i_qty - d_qty) <= 0.001:
                    continue
                logger.error(
                    "POSITION_MISMATCH_DETECTED",
                    symbol=sym,
                    ib_qty=i_qty,
                    db_qty=d_qty,
                    diff=i_qty - d_qty,
                )
                self.runtime._emit_audit(
                    stage="position_mismatch_detected",
                    severity="error",
                    symbol=sym,
                    status="mismatch",
                    reason="broker_db_position_diverged",
                    payload={
                        "ib_qty": float(i_qty),
                        "db_qty": float(d_qty),
                        "diff": float(i_qty - d_qty),
                        "db_open_symbol": bool(sym in db_open_symbols),
                        "ib_open_symbol": bool(sym in ib_open_symbols),
                    },
                )
                await self.quarantine_broker_only_position(
                    symbol=sym,
                    ib_qty=i_qty,
                    db_qty=d_qty,
                    db_rows=db_rows_by_symbol.get(sym, []),
                    broker_truth_connected=broker_truth_connected,
                )
        except Exception as exc:
            logger.error("Position audit failed", error=str(exc))

    async def quarantine_broker_only_position(
        self,
        *,
        symbol: str,
        ib_qty: float,
        db_qty: float,
        db_rows: List[Dict[str, Any]],
        broker_truth_connected: Optional[bool] = None,
    ) -> bool:
        sym = str(symbol or "").strip().upper()
        if not sym or broker_truth_connected is False:
            return False
        if not is_internal_opt_symbol(sym):
            return False
        if abs(ib_qty) <= 0.001 or abs(db_qty) > 0.001:
            return False

        strategy_ids: Set[str] = {
            str((row or {}).get("strategy_id") or "").strip()
            for row in (db_rows or [])
            if str((row or {}).get("strategy_id") or "").strip()
        }
        try:
            recent_rows = await self.runtime.pg.fetchall(
                """
                SELECT DISTINCT strategy_id
                FROM (
                    SELECT strategy_id
                    FROM core.core_order
                    WHERE symbol = $1
                    UNION ALL
                    SELECT strategy_id
                    FROM core.core_position
                    WHERE symbol = $1
                ) s
                WHERE strategy_id IS NOT NULL
                  AND btrim(strategy_id) <> ''
                  AND strategy_id <> 'adopted_reconciler'
                """,
                sym,
            )
        except Exception as exc:
            logger.warning(
                "position_mismatch_quarantine_lookup_failed",
                symbol=sym,
                error=str(exc),
            )
            recent_rows = []
        for row in recent_rows or []:
            strategy_id = str((row or {}).get("strategy_id") or "").strip()
            if strategy_id:
                strategy_ids.add(strategy_id)

        if not strategy_ids:
            self.runtime._emit_audit(
                stage="position_mismatch_quarantine_skipped",
                severity="error",
                symbol=sym,
                status="unowned",
                reason="broker_only_position_without_strategy_owner",
                payload={
                    "ib_qty": float(ib_qty),
                    "db_qty": float(db_qty),
                    "is_option_symbol": True,
                },
            )
            return False

        if self.runtime.manual_intervention_lock_store is None:
            self.runtime._emit_audit(
                stage="position_mismatch_quarantine_skipped",
                severity="error",
                symbol=sym,
                status="lock_store_missing",
                reason="broker_only_position_without_quarantine_lock_store",
                payload={
                    "ib_qty": float(ib_qty),
                    "db_qty": float(db_qty),
                    "strategy_ids": sorted(strategy_ids),
                },
            )
            return False

        locked: List[str] = []
        for strategy_id in sorted(strategy_ids):
            try:
                await self.runtime.manual_intervention_lock_store.lock(
                    strategy_id=strategy_id,
                    symbol=sym,
                    ttl_sec=6 * 60 * 60,
                    reason="position_mismatch_quarantine",
                    context={
                        "ib_qty": float(ib_qty),
                        "db_qty": float(db_qty),
                        "symbol": sym,
                    },
                )
                locked.append(strategy_id)
            except Exception as exc:
                logger.warning(
                    "position_mismatch_quarantine_lock_failed",
                    strategy_id=strategy_id,
                    symbol=sym,
                    error=str(exc),
                )
        if not locked:
            return False
        self.runtime._emit_audit(
            stage="position_mismatch_quarantined",
            severity="error",
            symbol=sym,
            status="quarantined",
            reason="broker_only_position_requires_manual_intervention",
            payload={
                "ib_qty": float(ib_qty),
                "db_qty": float(db_qty),
                "strategy_ids": locked,
                "ttl_sec": 6 * 60 * 60,
            },
        )
        await self.runtime._upsert_order_quarantine(
            symbol=sym,
            broker_order_id="",
            perm_id=None,
            order_ref="",
            side=None,
            qty=float(ib_qty),
            order_type=None,
            source="runtime_reconciler_position_mismatch",
            reason="broker_only_position_requires_manual_intervention",
            payload={
                "db_qty": float(db_qty),
                "strategy_ids": locked,
            },
        )
        return True

    async def sync_positions_from_ib(self) -> None:
        logger.warning(
            "position_sync_from_ib_skipped",
            reason="broker_aggregate_position_must_not_overwrite_strategy_virtual_positions",
        )
        emit = getattr(self.runtime, "_emit_audit", None)
        if callable(emit):
            emit(
                stage="position_sync_skipped",
                severity="warning",
                status="manual_review_required",
                reason="broker_aggregate_position_must_not_overwrite_strategy_virtual_positions",
                payload={"must_not_mutate_virtual_positions": True},
            )
        return
