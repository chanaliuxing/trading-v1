from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from libs.logger import get_logger
from libs.options.internal_symbol import parse_internal_opt_symbol
from services.exec.models import BrokerOrderAdjudicationStatus, Fill
from services.orders.ledger import PositionLedger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConvergenceResult:
    ok: bool
    reason: str
    metadata: Dict[str, Any]


class BrokerTruthConvergenceService:
    """Append-only convergence from broker facts into DB truth."""

    def __init__(
        self,
        *,
        pg: Any,
        position_ledger: Optional[PositionLedger] = None,
        account_id: Optional[str] = None,
        adopted_strategy_id: str = "adopted_reconciler",
    ) -> None:
        self.pg = pg
        self.position_ledger = position_ledger or (PositionLedger(pg=pg, account_id=account_id) if pg is not None else None)
        self.account_id = str(account_id or os.getenv("IB_ACCOUNT") or "UNKNOWN_ACCOUNT").strip() or "UNKNOWN_ACCOUNT"
        self.adopted_strategy_id = str(adopted_strategy_id or "adopted_reconciler").strip()

    async def converge_unattributed_fill(self, *, fill: Fill, side: Optional[str] = None) -> ConvergenceResult:
        if self.pg is None or self.position_ledger is None:
            return ConvergenceResult(False, "convergence_unavailable", {})
        exec_id = str(fill.exec_id or "").strip()
        if not exec_id:
            return ConvergenceResult(False, "missing_exec_id", {"broker_order_id": fill.order_id})
        side_u = self._normalize_side(side)
        if side_u not in {"BUY", "SELL"}:
            return ConvergenceResult(False, "missing_fill_side", {"exec_id": exec_id, "broker_order_id": fill.order_id})
        try:
            qty = abs(float(fill.qty or 0.0))
            price = float(fill.price or 0.0)
        except Exception:
            return ConvergenceResult(False, "invalid_fill_qty_or_price", {"exec_id": exec_id})
        if qty <= 0.0 or price <= 0.0:
            return ConvergenceResult(False, "invalid_fill_qty_or_price", {"exec_id": exec_id, "qty": qty, "price": price})
        if not self.account_id or self.account_id.upper() == "UNKNOWN_ACCOUNT":
            await self._quarantine_unknown_fill(
                fill=fill,
                side=side_u,
                qty=qty,
                price=price,
                reason="account_identity_unavailable",
                candidates=None,
            )
            return ConvergenceResult(
                False,
                "account_identity_unavailable",
                {"exec_id": exec_id, "broker_order_id": fill.order_id, "account_id": self.account_id},
            )

        order_row = await self._resolve_or_create_adopted_order(fill=fill, side=side_u, qty=qty)
        order_uuid = str((order_row or {}).get("order_id") or "").strip()
        if not order_uuid:
            reason = str((order_row or {}).get("_quarantine_reason") or "unattributed_broker_fill")
            candidates = (order_row or {}).get("_candidates")
            await self._quarantine_unknown_fill(
                fill=fill,
                side=side_u,
                qty=qty,
                price=price,
                reason=reason,
                candidates=candidates if isinstance(candidates, list) else None,
            )
            return ConvergenceResult(
                False,
                "ambiguous_fill_quarantined" if reason == "ambiguous_broker_fill" else "unattributed_fill_quarantined",
                {
                    "exec_id": exec_id,
                    "broker_order_id": fill.order_id,
                    "perm_id": fill.perm_id,
                    "symbol": str(fill.symbol or "").strip().upper(),
                    "candidate_count": len(candidates or []) if isinstance(candidates, list) else 0,
                },
            )

        ts = datetime.fromtimestamp(float(fill.ts_us or 0) / 1e6, tz=timezone.utc)
        owner = self._owner_from_order_row(order_row or {})
        if not self._owner_identity_complete(owner):
            await self._quarantine_unknown_fill(
                fill=fill,
                side=side_u,
                qty=qty,
                price=price,
                reason="owner_identity_incomplete",
                candidates=[order_row] if isinstance(order_row, dict) else None,
            )
            return ConvergenceResult(
                False,
                "owner_identity_incomplete",
                {
                    "order_id": order_uuid,
                    "exec_id": exec_id,
                    "broker_order_id": fill.order_id,
                    "perm_id": fill.perm_id,
                    "symbol": str(fill.symbol or "").strip().upper(),
                    "ledger_inserted": False,
                    "pnl_eligible": False,
                },
            )
        attribution_recorded = await self._record_fill_attribution(
            fill=fill,
            side=side_u,
            qty=qty,
            price=price,
            reason="verified_existing_core_order",
            attribution_status="verified",
            matched_core_order_id=order_uuid,
            matched_strategy_id=str((order_row or {}).get("strategy_id") or self.adopted_strategy_id),
            candidates=[order_row],
            pnl_eligible=True,
            ledger_eligible=True,
            manual_adjudication_required=False,
            event_ts=ts,
        )
        if not attribution_recorded:
            return ConvergenceResult(
                False,
                "fill_attribution_ledger_unavailable",
                {
                    "exec_id": exec_id,
                    "broker_order_id": fill.order_id,
                    "perm_id": fill.perm_id,
                    "symbol": str(fill.symbol or "").strip().upper(),
                },
            )
        fill_inserted = await self._insert_core_fill(fill=fill, order_uuid=order_uuid, ts=ts, qty=qty, price=price)
        ledger_inserted = await self.position_ledger.append_fill_event(
            strategy_id=str((order_row or {}).get("strategy_id") or self.adopted_strategy_id),
            strategy_instance_id=owner.get("strategy_instance_id"),
            symbol=str((order_row or {}).get("symbol") or fill.symbol or "").strip().upper(),
            side=side_u,
            qty=qty,
            price=price,
            event_ts=ts,
            commission=float(fill.commission or 0.0),
            broker_order_id=fill.order_id,
            ib_perm_id=fill.perm_id,
            exec_id=exec_id,
            idempotency_key=str((order_row or {}).get("idempotency_key") or self._fill_idempotency_key(exec_id)),
            order_ref=str((order_row or {}).get("order_ref") or fill.order_ref or ""),
            lot_id=owner.get("lot_id"),
            position_owner_key=owner.get("position_owner_key"),
            order_role=owner.get("order_role"),
            metadata={
                "core_order_id": order_uuid,
                "source": "broker_truth_convergence",
                "broker_order_id": fill.order_id,
                "perm_id": fill.perm_id,
                "strategy_instance_id": owner.get("strategy_instance_id"),
                "lot_id": owner.get("lot_id"),
                "position_owner_key": owner.get("position_owner_key"),
            },
            source="broker_truth_convergence_fill",
        )
        await self._mark_order_filled(order_uuid=order_uuid)
        await self._resolve_matching_quarantine(symbol=str(fill.symbol or ""), broker_order_id=fill.order_id, perm_id=fill.perm_id)
        return ConvergenceResult(
            True,
            "unattributed_fill_converged",
            {
                "order_id": order_uuid,
                "exec_id": exec_id,
                "fill_inserted": fill_inserted,
                "ledger_inserted": ledger_inserted,
            },
        )

    async def converge_position_mismatch(
        self,
        *,
        symbol: str,
        ib_qty: float,
        ledger_qty: float,
        strategy_id: Optional[str] = None,
        reason: str = "broker_position_truth_adjustment",
    ) -> ConvergenceResult:
        if self.pg is None or self.position_ledger is None:
            return ConvergenceResult(False, "convergence_unavailable", {})
        sym = str(symbol or "").strip().upper()
        if not sym:
            return ConvergenceResult(False, "missing_symbol", {})
        diff = float(ib_qty) - float(ledger_qty)
        if abs(diff) <= 0.001:
            return ConvergenceResult(True, "position_already_converged", {"symbol": sym, "diff": diff})
        sid = str(strategy_id or self.adopted_strategy_id).strip()
        if sid == self.adopted_strategy_id:
            await self._ensure_adopted_strategy()
        if abs(float(ib_qty)) <= 0.001 and abs(float(ledger_qty)) > 0.001:
            lot_result = await self._converge_broker_flat_lots(
                strategy_id=sid,
                symbol=sym,
                ledger_qty=float(ledger_qty),
                reason=reason,
            )
            if lot_result is not None:
                await self._resolve_position_quarantine(symbol=sym)
                return lot_result
        ledger_snapshot_token = await self._position_ledger_snapshot_token(
            strategy_id=sid,
            symbol=sym,
        )
        idem = self._position_adjustment_idempotency_key(
            strategy_id=sid,
            symbol=sym,
            ib_qty=ib_qty,
            ledger_qty=ledger_qty,
            ledger_snapshot_token=ledger_snapshot_token,
        )
        inserted = await self.position_ledger.append_adjustment_event(
            strategy_id=sid,
            symbol=sym,
            qty_delta=diff,
            event_ts=datetime.now(timezone.utc),
            idempotency_key=idem,
            metadata={
                "reason": reason,
                "ib_qty": float(ib_qty),
                "ledger_qty": float(ledger_qty),
                "diff": float(diff),
                "source": "broker_truth_convergence",
                "ledger_snapshot_token": ledger_snapshot_token,
            },
            source="broker_truth_convergence_position_adjustment",
        )
        await self._resolve_position_quarantine(symbol=sym)
        return ConvergenceResult(
            True,
            "position_mismatch_adjusted" if inserted else "position_mismatch_adjustment_already_exists",
            {
                "symbol": sym,
                "ib_qty": float(ib_qty),
                "ledger_qty": float(ledger_qty),
                "diff": float(diff),
                "inserted": inserted,
                "ledger_snapshot_token": ledger_snapshot_token,
            },
        )

    async def _converge_broker_flat_lots(
        self,
        *,
        strategy_id: str,
        symbol: str,
        ledger_qty: float,
        reason: str,
    ) -> Optional[ConvergenceResult]:
        if self.pg is None or self.position_ledger is None:
            return None
        account_id = str(getattr(self.position_ledger, "account_id", "") or "").strip()
        if not account_id or account_id == "UNKNOWN_ACCOUNT":
            return None
        fetchall = getattr(self.pg, "fetchall", None)
        if fetchall is None:
            return None
        rows = await fetchall(
            """
            SELECT COALESCE(lot_id, '') AS lot_id,
                   COALESCE(strategy_instance_id, '') AS strategy_instance_id,
                   COALESCE(position_owner_key, '') AS position_owner_key,
                   COALESCE(SUM(qty), 0) AS qty
            FROM core.position_ledger_current
            WHERE account_id = $1
              AND strategy_id = $2
              AND symbol = $3
            GROUP BY
                COALESCE(lot_id, ''),
                COALESCE(strategy_instance_id, ''),
                COALESCE(position_owner_key, '')
            HAVING ABS(COALESCE(SUM(qty), 0)) > 0.000001
            ORDER BY lot_id, strategy_instance_id
            """,
            account_id,
            str(strategy_id or "").strip(),
            str(symbol or "").strip().upper(),
        )
        if not rows:
            return None
        inserted_count = 0
        adjustments: list[dict[str, Any]] = []
        snapshot = await self._position_ledger_snapshot_token(strategy_id=strategy_id, symbol=symbol)
        for row in rows:
            lot_id = str(row.get("lot_id") or "").strip()
            strategy_instance_id = str(row.get("strategy_instance_id") or "").strip()
            position_owner_key = str(row.get("position_owner_key") or "").strip()
            qty = float(row.get("qty") or 0.0)
            if abs(qty) <= 0.001:
                continue
            lot_key = lot_id or "NULL_LOT"
            instance_key = strategy_instance_id or "NULL_INSTANCE"
            idem = (
                "broker_truth:position_lot_flat:"
                f"{account_id}:{strategy_id}:{symbol}:{instance_key}:{lot_key}:{float(qty):.8f}:{snapshot}"
            )
            inserted = await self.position_ledger.append_adjustment_event(
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance_id or None,
                symbol=symbol,
                lot_id=lot_id or None,
                position_owner_key=position_owner_key or None,
                qty_delta=-qty,
                event_ts=datetime.now(timezone.utc),
                idempotency_key=idem,
                metadata={
                    "reason": reason,
                    "ib_qty": 0.0,
                    "ledger_qty": float(ledger_qty),
                    "lot_qty": float(qty),
                    "lot_id": lot_id or None,
                    "strategy_instance_id": strategy_instance_id or None,
                    "position_owner_key": position_owner_key or None,
                    "source": "broker_truth_convergence",
                    "ledger_snapshot_token": snapshot,
                },
                source="broker_truth_convergence_position_lot_adjustment",
            )
            if inserted:
                inserted_count += 1
            adjustments.append(
                {
                    "lot_id": lot_id or None,
                    "strategy_instance_id": strategy_instance_id or None,
                    "position_owner_key": position_owner_key or None,
                    "previous_net_qty": qty,
                    "adjustment_qty": -qty,
                    "inserted": bool(inserted),
                    "idempotency_key": idem,
                }
            )
        return ConvergenceResult(
            True,
            "position_lots_adjusted_to_broker_flat" if inserted_count else "position_lot_adjustments_already_exist",
            {
                "symbol": symbol,
                "ib_qty": 0.0,
                "ledger_qty": float(ledger_qty),
                "inserted_count": int(inserted_count),
                "adjustments": adjustments,
                "ledger_snapshot_token": snapshot,
            },
        )

    async def _position_ledger_snapshot_token(self, *, strategy_id: str, symbol: str) -> str:
        if self.pg is None:
            return "unavailable"
        try:
            row = await self.pg.fetchone(
                """
                SELECT
                    COUNT(*)::int AS event_count,
                    MAX(created_at) AS max_created_at,
                    MAX(event_ts) AS max_event_ts,
                    COALESCE(SUM(qty_delta), 0) AS net_qty
                FROM core.position_ledger_event
                WHERE strategy_id = $1
                  AND symbol = $2
                """,
                str(strategy_id or "").strip(),
                str(symbol or "").strip().upper(),
            )
        except Exception as exc:
            logger.warning(
                "position_ledger_snapshot_token_lookup_failed",
                strategy_id=str(strategy_id or ""),
                symbol=str(symbol or ""),
                error=str(exc),
            )
            return "lookup_failed"
        if not row:
            return "empty"
        get = row.get if hasattr(row, "get") else lambda key, default=None: default
        max_created_at = get("max_created_at")
        max_event_ts = get("max_event_ts")
        created_token = max_created_at.isoformat() if hasattr(max_created_at, "isoformat") else str(max_created_at or "")
        event_token = max_event_ts.isoformat() if hasattr(max_event_ts, "isoformat") else str(max_event_ts or "")
        return (
            f"events={int(get('event_count') or 0)}"
            f":created={created_token}"
            f":event={event_token}"
            f":net={float(get('net_qty') or 0.0):.8f}"
        )

    async def _ensure_adopted_strategy(self) -> None:
        await self.pg.execute(
            """
            INSERT INTO core.core_strategy (strategy_id, name, capital_limit_usd, reserve_buffer_bps, is_enabled, created_at)
            VALUES ($1, 'Adopted by Broker Truth Convergence', 1000000, 10, true, now())
            ON CONFLICT (strategy_id) DO NOTHING
            """,
            self.adopted_strategy_id,
        )

    async def _resolve_or_create_adopted_order(self, *, fill: Fill, side: str, qty: float) -> Dict[str, Any]:
        broker_order_id = str(fill.order_id or "").strip()
        perm_id = self._optional_int(fill.perm_id)
        rows = await self.pg.fetchall(
            """
            SELECT order_id, account_id, strategy_id, symbol, side, qty, idempotency_key, order_ref,
                   state, created_at, submitted_at, updated_at, note
            FROM core.core_order
            WHERE account_id = $3
              AND (
                    ($1::text <> '' AND broker_order_id = $1)
                 OR ($2::bigint IS NOT NULL AND ib_perm_id = $2)
              )
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            """,
            broker_order_id,
            perm_id,
            self.account_id,
        )
        candidates = [dict(row) for row in (rows or []) if hasattr(row, "get")]
        fill_symbol = str(fill.symbol or "").strip().upper()
        fill_order_ref = str(fill.order_ref or "").strip()
        strong: list[dict[str, Any]] = []
        for row in candidates:
            row_symbol = str(row.get("symbol") or "").strip().upper()
            row_account = str(row.get("account_id") or "").strip()
            row_side = str(row.get("side") or "").strip().upper()
            row_order_ref = str(row.get("order_ref") or "").strip()
            row_state = str(row.get("state") or "").strip().upper()
            try:
                row_qty = abs(float(row.get("qty") or 0.0))
            except Exception:
                row_qty = 0.0
            order_ref_matches = not fill_order_ref or row_order_ref == fill_order_ref
            if (
                row_symbol == fill_symbol
                and row_account == self.account_id
                and row_side == side
                and row_qty + 1e-6 >= qty
                and order_ref_matches
                and row_state in {"SUBMITTED", "PARTIAL", "FILLED"}
                and self._fill_time_matches_order(fill=fill, order=row)
                and self._option_contract_identity_matches(fill_symbol=fill_symbol, order_symbol=row_symbol)
            ):
                strong.append(row)
        if len(strong) == 1:
            return strong[0]
        if candidates:
            return {"_quarantine_reason": "ambiguous_broker_fill", "_candidates": candidates}

        return {}

    @staticmethod
    def _owner_from_order_row(order_row: dict[str, Any]) -> dict[str, Any]:
        note_payload: dict[str, Any] = {}
        raw_note = order_row.get("note")
        if isinstance(raw_note, str) and raw_note.strip():
            try:
                parsed = json.loads(raw_note)
                if isinstance(parsed, dict):
                    note_payload = parsed
            except Exception:
                note_payload = {}
        elif isinstance(raw_note, dict):
            note_payload = raw_note
        extras = note_payload.get("extras") if isinstance(note_payload.get("extras"), dict) else {}
        envelope_extras = (
            note_payload.get("envelope_extras")
            if isinstance(note_payload.get("envelope_extras"), dict)
            else {}
        )
        merged = {
            **note_payload,
            **extras,
            **envelope_extras,
            **{key: value for key, value in order_row.items() if value is not None},
        }
        strategy_id = str(order_row.get("strategy_id") or merged.get("strategy_id") or "").strip()
        strategy_instance_id = str(merged.get("strategy_instance_id") or "").strip()
        symbol = str(order_row.get("symbol") or merged.get("symbol") or "").strip().upper()
        lot_id = str(merged.get("position_lot_id") or merged.get("lot_id") or "").strip()
        position_owner_key = str(merged.get("position_owner_key") or "").strip()
        if not position_owner_key and strategy_id and strategy_instance_id and symbol and lot_id:
            position_owner_key = f"{strategy_id}:{strategy_instance_id}:{symbol}:{lot_id}"
        return {
            "strategy_instance_id": strategy_instance_id or None,
            "lot_id": lot_id or None,
            "position_owner_key": position_owner_key or None,
            "order_role": str(merged.get("order_role") or merged.get("exit_order_role") or "").strip().upper() or None,
        }

    @staticmethod
    def _owner_identity_complete(owner: dict[str, Any]) -> bool:
        return bool(
            str(owner.get("strategy_instance_id") or "").strip()
            and str(owner.get("lot_id") or "").strip()
            and str(owner.get("position_owner_key") or "").strip()
        )

    async def _quarantine_unknown_fill(
        self,
        *,
        fill: Fill,
        side: str,
        qty: float,
        price: float,
        reason: str = "unattributed_broker_fill",
        candidates: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        exec_id = str(fill.exec_id or "").strip()
        note = json.dumps(
            {
                "source": "broker_truth_convergence",
                "reason": reason,
                "adjudication_status": BrokerOrderAdjudicationStatus.UNKNOWN_OWNER.value,
                "manual_required": True,
                "exec_id": exec_id,
                "broker_order_id": str(fill.order_id or "").strip() or None,
                "perm_id": self._optional_int(fill.perm_id),
                "order_ref": str(fill.order_ref or "").strip() or None,
                "side": side,
                "qty": float(qty),
                "price": float(price),
                "account_required": True,
                "strong_unique_match_required": True,
                "candidate_count": len(candidates or []),
                "candidate_samples": [
                    {
                        "order_id": str(item.get("order_id") or ""),
                        "strategy_id": str(item.get("strategy_id") or ""),
                        "symbol": str(item.get("symbol") or ""),
                        "side": str(item.get("side") or ""),
                        "qty": float(item.get("qty") or 0.0),
                        "state": str(item.get("state") or ""),
                        "created_at": self._iso_or_none(item.get("created_at")),
                        "submitted_at": self._iso_or_none(item.get("submitted_at")),
                        "updated_at": self._iso_or_none(item.get("updated_at")),
                    }
                    for item in (candidates or [])[:5]
                ],
                "ledger_adoption_blocked": True,
                "time_window_required": True,
                "option_contract_identity_required": str(fill.symbol or "").strip().upper().startswith("OPT:"),
            },
            ensure_ascii=False,
        )
        symbol = str(fill.symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
        broker_order_id = str(fill.order_id or "").strip()
        perm_id = self._optional_int(fill.perm_id)
        order_ref = str(fill.order_ref or "").strip()
        if perm_id is not None:
            await self.pg.execute(
                """
                INSERT INTO core.core_order_quarantine (
                    symbol, broker_order_id, perm_id, ib_perm_id, order_ref,
                    reason, status, note, observed_at, created_at, updated_at
                ) VALUES (
                    $1, NULLIF($2, ''), $3, $3, NULLIF($4, ''),
                    $6, 'active', $5, now(), now(), now()
                )
                ON CONFLICT (ib_perm_id)
                WHERE status = 'active' AND ib_perm_id IS NOT NULL
                DO UPDATE
                SET symbol = EXCLUDED.symbol,
                    broker_order_id = COALESCE(EXCLUDED.broker_order_id, core.core_order_quarantine.broker_order_id),
                    order_ref = COALESCE(EXCLUDED.order_ref, core.core_order_quarantine.order_ref),
                    reason = EXCLUDED.reason,
                    note = EXCLUDED.note,
                    observed_at = now(),
                    updated_at = now()
                """,
                symbol,
                broker_order_id,
                perm_id,
                order_ref,
                note,
                reason,
            )
            await self._record_fill_attribution(
                fill=fill,
                side=side,
                qty=qty,
                price=price,
                reason=reason,
                attribution_status="ambiguous" if reason == "ambiguous_broker_fill" else "unknown",
                candidates=candidates or [],
            )
            return
        if broker_order_id:
            await self.pg.execute(
                """
                INSERT INTO core.core_order_quarantine (
                    symbol, broker_order_id, perm_id, ib_perm_id, order_ref,
                    reason, status, note, observed_at, created_at, updated_at
                ) VALUES (
                    $1, NULLIF($2, ''), NULL, NULL, NULLIF($3, ''),
                    $5, 'active', $4, now(), now(), now()
                )
                ON CONFLICT (broker_order_id)
                WHERE status = 'active' AND broker_order_id IS NOT NULL
                DO UPDATE
                SET symbol = EXCLUDED.symbol,
                    order_ref = COALESCE(EXCLUDED.order_ref, core.core_order_quarantine.order_ref),
                    reason = EXCLUDED.reason,
                    note = EXCLUDED.note,
                    observed_at = now(),
                    updated_at = now()
                """,
                symbol,
                broker_order_id,
                order_ref,
                note,
                reason,
            )
            await self._record_fill_attribution(
                fill=fill,
                side=side,
                qty=qty,
                price=price,
                reason=reason,
                attribution_status="ambiguous" if reason == "ambiguous_broker_fill" else "unknown",
                candidates=candidates or [],
            )
            return
        await self.pg.execute(
            """
            INSERT INTO core.core_order_quarantine (
                symbol, broker_order_id, perm_id, ib_perm_id, order_ref,
                reason, status, note, observed_at, created_at, updated_at
            ) VALUES (
                $1, NULL, NULL, NULL, NULLIF($2, ''),
                $4, 'active', $3, now(), now(), now()
            )
            """,
            symbol,
            order_ref,
            note,
            reason,
        )
        await self._record_fill_attribution(
            fill=fill,
            side=side,
            qty=qty,
            price=price,
            reason=reason,
            attribution_status="ambiguous" if reason == "ambiguous_broker_fill" else "unknown",
            candidates=candidates or [],
        )

    async def _record_fill_attribution(
        self,
        *,
        fill: Fill,
        side: str,
        qty: float,
        price: float,
        reason: str,
        attribution_status: str,
        candidates: Optional[list[dict[str, Any]]] = None,
        matched_core_order_id: Optional[str] = None,
        matched_strategy_id: Optional[str] = None,
        pnl_eligible: bool = False,
        ledger_eligible: bool = False,
        manual_adjudication_required: bool = True,
        event_ts: Optional[datetime] = None,
    ) -> bool:
        try:
            from services.production.fill_attribution_service import ensure_fill_attribution_ledger_schema

            await ensure_fill_attribution_ledger_schema(pg=self.pg)
            exec_id = str(fill.exec_id or "").strip()
            symbol = str(fill.symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
            asset_class = "OPT" if symbol.startswith("OPT:") else "STK"
            broker_order_id = str(fill.order_id or "").strip()
            order_ref = str(fill.order_ref or "").strip()
            perm_id = self._optional_int(fill.perm_id)
            ts = event_ts
            if ts is None and fill.ts_us:
                ts = datetime.fromtimestamp(float(fill.ts_us or 0) / 1e6, tz=timezone.utc)
            identity = exec_id or broker_order_id or str(perm_id or "") or order_ref or "no_identity"
            event_key = f"{self.account_id}:{symbol}:{identity}:{side}:{float(qty):.8f}:{float(price):.8f}"
            candidate_snapshot = json.dumps(
                [
                    {
                        "order_id": str(item.get("order_id") or ""),
                        "strategy_id": str(item.get("strategy_id") or ""),
                        "symbol": str(item.get("symbol") or ""),
                        "side": str(item.get("side") or ""),
                        "qty": float(item.get("qty") or 0.0),
                        "order_ref": str(item.get("order_ref") or ""),
                    }
                    for item in (candidates or [])[:20]
                ],
                ensure_ascii=False,
            )
            await self.pg.execute(
                """
                INSERT INTO core.fill_attribution_ledger (
                    event_key, account, symbol, asset_class, broker_order_id, perm_id,
                    order_ref, exec_id, side, qty, price, event_ts,
                    attribution_status, attribution_reason, matched_core_order_id,
                    matched_strategy_id, candidate_count, candidate_snapshot,
                    pnl_eligible, ledger_eligible, manual_adjudication_required,
                    created_at, updated_at
                ) VALUES (
                    $1, NULLIF($2, ''), $3, $4, NULLIF($5, ''), $6,
                    NULLIF($7, ''), NULLIF($8, ''), $9, $10, $11, $12,
                    $13, $14, $15::uuid, NULLIF($16, ''), $17, $18::jsonb,
                    $19, $20, $21,
                    now(), now()
                )
                ON CONFLICT (event_key)
                DO UPDATE
                SET account = COALESCE(EXCLUDED.account, core.fill_attribution_ledger.account),
                    symbol = EXCLUDED.symbol,
                    broker_order_id = COALESCE(EXCLUDED.broker_order_id, core.fill_attribution_ledger.broker_order_id),
                    perm_id = COALESCE(EXCLUDED.perm_id, core.fill_attribution_ledger.perm_id),
                    order_ref = COALESCE(EXCLUDED.order_ref, core.fill_attribution_ledger.order_ref),
                    exec_id = COALESCE(EXCLUDED.exec_id, core.fill_attribution_ledger.exec_id),
                    side = EXCLUDED.side,
                    qty = EXCLUDED.qty,
                    price = EXCLUDED.price,
                    event_ts = COALESCE(EXCLUDED.event_ts, core.fill_attribution_ledger.event_ts),
                    attribution_status = EXCLUDED.attribution_status,
                    attribution_reason = EXCLUDED.attribution_reason,
                    matched_core_order_id = COALESCE(EXCLUDED.matched_core_order_id, core.fill_attribution_ledger.matched_core_order_id),
                    matched_strategy_id = COALESCE(EXCLUDED.matched_strategy_id, core.fill_attribution_ledger.matched_strategy_id),
                    candidate_count = EXCLUDED.candidate_count,
                    candidate_snapshot = EXCLUDED.candidate_snapshot,
                    pnl_eligible = EXCLUDED.pnl_eligible,
                    ledger_eligible = EXCLUDED.ledger_eligible,
                    manual_adjudication_required = EXCLUDED.manual_adjudication_required,
                    updated_at = now()
                """,
                event_key,
                self.account_id,
                symbol,
                asset_class,
                broker_order_id,
                perm_id,
                order_ref,
                exec_id,
                side,
                float(qty),
                float(price),
                ts,
                attribution_status,
                reason,
                matched_core_order_id or None,
                matched_strategy_id or "",
                len(candidates or []),
                candidate_snapshot,
                bool(pnl_eligible),
                bool(ledger_eligible),
                bool(manual_adjudication_required),
            )
            return True
        except Exception as exc:
            logger.error(
                "fill_attribution_ledger_record_failed",
                error=str(exc),
                symbol=str(fill.symbol or "").strip().upper(),
                exec_id=str(fill.exec_id or "").strip(),
                reason=reason,
                exc_info=True,
            )
            return False

    async def _insert_core_fill(self, *, fill: Fill, order_uuid: str, ts: datetime, qty: float, price: float) -> bool:
        row = await self.pg.fetchone(
            """
            INSERT INTO core.core_fill (
                fill_id, order_id, ts, qty, price,
                liquidity, commission_usd, exchange, exec_id, created_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4,
                $5, $6, $7, $8, now()
            )
            ON CONFLICT (order_id, ts, qty, price) DO NOTHING
            RETURNING 1 AS inserted
            """,
            order_uuid,
            ts,
            qty,
            price,
            fill.liquidity or "UNKNOWN",
            float(fill.commission or 0.0),
            fill.exchange,
            str(fill.exec_id or "").strip(),
        )
        return bool(row and (not hasattr(row, "get") or row.get("inserted") == 1))

    async def _mark_order_filled(self, *, order_uuid: str) -> None:
        await self.pg.execute(
            """
            UPDATE core.core_order
            SET state = CASE WHEN state IN ('NEW', 'SUBMITTED', 'PARTIAL') THEN 'FILLED' ELSE state END,
                updated_at = now()
            WHERE order_id = $1
            """,
            order_uuid,
        )

    async def _resolve_matching_quarantine(self, *, symbol: str, broker_order_id: Any, perm_id: Any) -> None:
        await self.pg.execute(
            """
            UPDATE core.core_order_quarantine
            SET status = 'resolved',
                resolved_at = now(),
                updated_at = now()
            WHERE status = 'active'
              AND symbol = $1
              AND reason IN ('unattributed_broker_fill', 'ambiguous_broker_fill', 'unknown_broker_order_detected')
              AND (
                    (NULLIF($2, '') IS NOT NULL AND broker_order_id = NULLIF($2, ''))
                 OR ($3::bigint IS NOT NULL AND (ib_perm_id = $3 OR perm_id = $3))
              )
            """,
            str(symbol or "").strip().upper() or "UNKNOWN",
            str(broker_order_id or "").strip(),
            self._optional_int(perm_id),
        )

    async def _resolve_position_quarantine(self, *, symbol: str) -> None:
        await self.pg.execute(
            """
            UPDATE core.core_order_quarantine
            SET status = 'resolved',
                resolved_at = now(),
                updated_at = now()
            WHERE status = 'active'
              AND symbol = $1
              AND reason = 'broker_only_position_requires_manual_intervention'
            """,
            str(symbol or "").strip().upper(),
        )

    @staticmethod
    def _fill_idempotency_key(exec_id: str) -> str:
        return f"broker_truth:fill:{str(exec_id or '').strip()}"

    def _position_adjustment_idempotency_key(
        self,
        *,
        strategy_id: str,
        symbol: str,
        ib_qty: float,
        ledger_qty: float,
        ledger_snapshot_token: str = "",
    ) -> str:
        account_id = str(getattr(self.position_ledger, "account_id", "") or "UNKNOWN_ACCOUNT") if self.position_ledger else "UNKNOWN_ACCOUNT"
        owner = str(strategy_id or "").strip() or self.adopted_strategy_id
        snapshot = str(ledger_snapshot_token or "snapshot_unknown").strip()
        return f"broker_truth:position:{account_id}:{owner}:{symbol}:{float(ib_qty):.8f}:{float(ledger_qty):.8f}:{snapshot}"

    @staticmethod
    def _normalize_side(side: Optional[str]) -> str:
        raw = str(side or "").strip().upper()
        if raw in {"BOT", "BUY", "BOUGHT"}:
            return "BUY"
        if raw in {"SLD", "SELL", "SOLD"}:
            return "SELL"
        return raw

    @staticmethod
    def _fill_time_matches_order(*, fill: Fill, order: dict[str, Any], window: timedelta = timedelta(hours=36)) -> bool:
        fill_ts = None
        try:
            if fill.ts_us:
                fill_ts = datetime.fromtimestamp(float(fill.ts_us or 0) / 1e6, tz=timezone.utc)
        except Exception:
            fill_ts = None
        if fill_ts is None:
            return False
        order_times: list[datetime] = []
        for key in ("submitted_at", "updated_at", "created_at"):
            parsed = BrokerTruthConvergenceService._parse_dt(order.get(key))
            if parsed is not None:
                order_times.append(parsed)
        if not order_times:
            return False
        earliest = min(order_times)
        latest = max(order_times)
        return earliest - window <= fill_ts <= latest + window

    @staticmethod
    def _option_contract_identity_matches(*, fill_symbol: str, order_symbol: str) -> bool:
        fill_u = str(fill_symbol or "").strip().upper()
        order_u = str(order_symbol or "").strip().upper()
        if fill_u != order_u:
            return False
        if not fill_u.startswith("OPT:") and not order_u.startswith("OPT:"):
            return True
        try:
            fill_parts = parse_internal_opt_symbol(fill_u)
            order_parts = parse_internal_opt_symbol(order_u)
        except Exception:
            return False
        return (
            fill_parts.underlying == order_parts.underlying
            and fill_parts.expiry_ymd == order_parts.expiry_ymd
            and abs(float(fill_parts.strike) - float(order_parts.strike)) <= 1e-9
            and fill_parts.right == order_parts.right
        )

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _iso_or_none(value: Any) -> Optional[str]:
        parsed = BrokerTruthConvergenceService._parse_dt(value)
        return parsed.isoformat() if parsed is not None else None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None or str(value).strip() == "":
            return None
        try:
            ivalue = int(value)
            return ivalue if ivalue > 0 else None
        except Exception:
            return None
