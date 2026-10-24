from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from libs.logger import get_logger

logger = get_logger(__name__)


class PositionLedger:
    """Append-only position and financial event writer."""

    def __init__(self, *, pg: Any, account_id: Optional[str] = None) -> None:
        self.pg = pg
        self.account_id = str(account_id or os.getenv("IB_ACCOUNT", "") or "UNKNOWN_ACCOUNT")

    async def ensure_schema(self) -> None:
        if self.pg is None:
            return
        ddl_path = Path("db/pg/migration_20260421_uom_position_ledger.ddl")
        statements = self._split_ddl_statements(ddl_path.read_text(encoding="utf-8"))
        deduped = False
        for statement in statements:
            if (not deduped) and "uq_position_ledger_exec" in statement:
                await self._dedupe_existing_unique_keys()
                deduped = True
            if statement.upper().startswith("CREATE OR REPLACE VIEW CORE.POSITION_LEDGER_CURRENT"):
                # Postgres cannot use CREATE OR REPLACE VIEW when a view column
                # order/name changes. This view is a projection over append-only
                # ledger events, so dropping it first is safe and makes schema
                # evolution idempotent across live restarts.
                await self.pg.execute("DROP VIEW IF EXISTS core.position_ledger_current")
            await self.pg.execute(statement)
        await self._bootstrap_current_state_if_empty()

    @staticmethod
    def _split_ddl_statements(sql: str) -> list[str]:
        statements: list[str] = []
        buf: list[str] = []
        dollar_tag: Optional[str] = None
        i = 0
        while i < len(sql):
            if sql[i] == "$":
                j = i + 1
                while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                    j += 1
                if j < len(sql) and sql[j] == "$":
                    tag = sql[i : j + 1]
                    if dollar_tag is None:
                        dollar_tag = tag
                    elif dollar_tag == tag:
                        dollar_tag = None
                    buf.append(tag)
                    i = j + 1
                    continue
            ch = sql[i]
            if ch == ";" and dollar_tag is None:
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
            else:
                buf.append(ch)
            i += 1
        tail = "".join(buf).strip()
        if tail:
            statements.append(tail)
        return statements

    async def _dedupe_existing_unique_keys(self) -> None:
        """Remove historical duplicate ledger rows before enabling idempotency indexes."""
        if self.pg is None:
            return
        row = await self.pg.fetchone(
            """
            WITH duplicate_rows AS (
                SELECT ledger_event_id
                FROM (
                    SELECT
                        ledger_event_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY account_id, exec_id
                            ORDER BY created_at ASC, event_ts ASC, ledger_event_id ASC
                        ) AS rn
                    FROM core.position_ledger_event
                    WHERE exec_id IS NOT NULL AND exec_id <> ''
                ) ranked
                WHERE rn > 1
                UNION
                SELECT ledger_event_id
                FROM (
                    SELECT
                        ledger_event_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY account_id, idempotency_key
                            ORDER BY created_at ASC, event_ts ASC, ledger_event_id ASC
                        ) AS rn
                    FROM core.position_ledger_event
                    WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''
                ) ranked
                WHERE rn > 1
            ),
            deleted AS (
                DELETE FROM core.position_ledger_event e
                USING duplicate_rows d
                WHERE e.ledger_event_id = d.ledger_event_id
                RETURNING e.ledger_event_id
            )
            SELECT COUNT(*)::int AS deleted_count FROM deleted
            """
        )
        deleted_count = int((row or {}).get("deleted_count") or 0)
        if deleted_count > 0:
            logger.warning(
                "position_ledger_duplicate_events_removed_before_unique_index",
                deleted_count=deleted_count,
            )

    async def _bootstrap_current_state_if_empty(self) -> None:
        """One-time migration bootstrap from historical events into bounded current lanes."""
        if self.pg is None:
            return
        try:
            row = await self.pg.fetchone(
                """
                SELECT
                    (SELECT COUNT(*)::int FROM core.position_ledger_current_state) AS current_count,
                    (SELECT COUNT(*)::int FROM core.position_ledger_event) AS event_count
                """
            )
            current_count = int((row or {}).get("current_count") or 0)
            event_count = int((row or {}).get("event_count") or 0)
            if event_count <= 0:
                return
            if current_count > 0:
                lanes = await self.pg.fetchall(
                    """
                    SELECT DISTINCT e.account_id, e.strategy_id, e.strategy_instance_id, e.symbol, e.lot_id
                    FROM core.position_ledger_event e
                    LEFT JOIN core.position_ledger_current_state c
                      ON c.account_id = e.account_id
                     AND c.strategy_id = e.strategy_id
                     AND (
                        (c.strategy_instance_id IS NULL AND e.strategy_instance_id IS NULL)
                        OR c.strategy_instance_id = e.strategy_instance_id
                     )
                     AND c.symbol = e.symbol
                     AND ((c.lot_id IS NULL AND e.lot_id IS NULL) OR c.lot_id = e.lot_id)
                    WHERE c.account_id IS NULL
                    ORDER BY e.account_id, e.strategy_id, e.symbol, e.lot_id
                    """
                )
            else:
                lanes = await self.pg.fetchall(
                    """
                    SELECT DISTINCT account_id, strategy_id, strategy_instance_id, symbol, lot_id
                    FROM core.position_ledger_event
                    ORDER BY account_id, strategy_id, strategy_instance_id, symbol, lot_id
                    """
                )
            if not lanes:
                return
        except Exception:
            logger.exception("position_ledger_current_state_bootstrap_probe_failed")
            return
        bootstrapped = 0
        for lane in lanes or []:
            lane_account = str(dict(lane).get("account_id") or "").strip()
            if lane_account and lane_account != self.account_id:
                lane_ledger = PositionLedger(pg=self.pg, account_id=lane_account)
                await lane_ledger.rebuild_current_state_for_lane(
                    strategy_id=dict(lane).get("strategy_id") or "",
                    strategy_instance_id=dict(lane).get("strategy_instance_id"),
                    symbol=dict(lane).get("symbol") or "",
                    lot_id=dict(lane).get("lot_id"),
                )
            else:
                await self.rebuild_current_state_for_lane(
                    strategy_id=dict(lane).get("strategy_id") or "",
                    strategy_instance_id=dict(lane).get("strategy_instance_id"),
                    symbol=dict(lane).get("symbol") or "",
                    lot_id=dict(lane).get("lot_id"),
                )
            bootstrapped += 1
        if bootstrapped > 0:
            logger.warning(
                "position_ledger_current_state_bootstrapped_from_events",
                lane_count=bootstrapped,
                event_count=event_count,
            )

    async def current_position(
        self,
        *,
        strategy_id: str,
        symbol: str,
        strategy_instance_id: Any = None,
        lot_id: Any = None,
    ) -> Dict[str, Any]:
        if self.pg is None:
            return {"qty": 0.0, "avg_px": None, "commission_sum": 0.0, "fees_sum": 0.0}
        lot_filter = "AND (($4::text IS NULL AND lot_id IS NULL) OR lot_id = $4)"
        rows = await self.pg.fetchall(
            f"""
            SELECT
                event_ts,
                qty_delta::float AS qty_delta,
                price::float AS price,
                COALESCE(commission, 0)::float AS commission,
                COALESCE(fees, 0)::float AS fees,
                event_type,
                created_at
            FROM core.position_ledger_event
            WHERE account_id = $1
              AND strategy_id = $2
              AND symbol = $3
              AND (($5::text IS NULL AND strategy_instance_id IS NULL) OR strategy_instance_id = $5)
              {lot_filter}
            ORDER BY event_ts ASC NULLS LAST, created_at ASC NULLS LAST, ledger_event_id ASC
            """,
            self.account_id,
            str(strategy_id or "").strip(),
            str(symbol or "").strip().upper(),
            (str(lot_id).strip() if lot_id is not None and str(lot_id).strip() else None),
            (str(strategy_instance_id).strip() if strategy_instance_id is not None and str(strategy_instance_id).strip() else None),
        )
        replay = self._replay_position_events([dict(row) for row in (rows or [])])
        return {
            "qty": replay["qty"],
            "avg_px": replay["avg_px"],
            "realized_pnl": replay["realized_pnl"],
            "commission_sum": replay["commission_sum"],
            "fees_sum": replay["fees_sum"],
            "last_event_ts": replay["last_event_ts"],
        }

    async def rebuild_current_state_for_lane(
        self,
        *,
        strategy_id: str,
        strategy_instance_id: Any = None,
        symbol: str,
        lot_id: Any = None,
    ) -> Dict[str, Any]:
        """Repair the materialized current ledger projection for one bounded lane."""
        return await self._refresh_current_state_for_lane(
            strategy_id=str(strategy_id or "").strip(),
            strategy_instance_id=self._normalize_owner_text(strategy_instance_id),
            symbol=str(symbol or "").strip().upper(),
            lot_id=self._normalize_lot_id(lot_id),
        )

    async def _refresh_current_state_for_lane(
        self,
        *,
        strategy_id: str,
        strategy_instance_id: Optional[str],
        symbol: str,
        lot_id: Optional[str],
    ) -> Dict[str, Any]:
        if self.pg is None:
            return {"qty": 0.0, "avg_px": None, "realized_pnl": 0.0, "commission_sum": 0.0, "fees_sum": 0.0, "last_event_ts": None}
        rows = await self.pg.fetchall(
            """
            SELECT
                event_ts,
                qty_delta::float AS qty_delta,
                price::float AS price,
                COALESCE(commission, 0)::float AS commission,
                COALESCE(fees, 0)::float AS fees,
                event_type,
                created_at
            FROM core.position_ledger_event
            WHERE account_id = $1
              AND strategy_id = $2
              AND symbol = $3
              AND (($4::text IS NULL AND lot_id IS NULL) OR lot_id = $4)
              AND (($5::text IS NULL AND strategy_instance_id IS NULL) OR strategy_instance_id = $5)
            ORDER BY event_ts ASC NULLS LAST, created_at ASC NULLS LAST, ledger_event_id ASC
            """,
            self.account_id,
            strategy_id,
            symbol,
            lot_id,
            strategy_instance_id,
        )
        replay = self._replay_position_events([dict(row) for row in (rows or [])])
        qty = float(replay["qty"])
        avg_px = replay["avg_px"]
        notional_sum = abs(qty) * float(avg_px) if abs(qty) > 1e-12 and avg_px is not None else None
        await self.pg.execute(
            """
            INSERT INTO core.position_ledger_current_state (
                account_id, strategy_id, symbol, lot_id, lot_key,
                qty, notional_sum, avg_px, realized_pnl,
                commission_sum, fees_sum, last_event_ts,
                strategy_instance_id, position_owner_key, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12,
                $13, $14, now()
            )
            ON CONFLICT (account_id, strategy_id, symbol, lot_key)
            DO UPDATE SET
                strategy_instance_id = EXCLUDED.strategy_instance_id,
                lot_id = EXCLUDED.lot_id,
                position_owner_key = EXCLUDED.position_owner_key,
                qty = EXCLUDED.qty,
                notional_sum = EXCLUDED.notional_sum,
                avg_px = EXCLUDED.avg_px,
                realized_pnl = EXCLUDED.realized_pnl,
                commission_sum = EXCLUDED.commission_sum,
                fees_sum = EXCLUDED.fees_sum,
                last_event_ts = EXCLUDED.last_event_ts,
                updated_at = now()
            """,
            self.account_id,
            strategy_id,
            symbol,
            lot_id,
            lot_id or "",
            qty,
            notional_sum,
            avg_px,
            float(replay["realized_pnl"]),
            float(replay["commission_sum"]),
            float(replay["fees_sum"]),
            replay["last_event_ts"],
            strategy_instance_id,
            self._position_owner_key(
                strategy_id=strategy_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                lot_id=lot_id,
            ),
        )
        return replay

    @staticmethod
    def _replay_position_events(rows: list[dict[str, Any]]) -> Dict[str, Any]:
        qty = 0.0
        avg_px: float | None = None
        realized_pnl = 0.0
        commission_sum = 0.0
        fees_sum = 0.0
        last_event_ts = None
        for row in rows:
            try:
                delta = float(row.get("qty_delta") or 0.0)
            except Exception:
                delta = 0.0
            price_raw = row.get("price")
            try:
                price = float(price_raw) if price_raw is not None else None
            except Exception:
                price = None
            try:
                commission_sum += float(row.get("commission") or 0.0)
                fees_sum += float(row.get("fees") or 0.0)
            except Exception:
                pass
            if row.get("event_ts") is not None:
                last_event_ts = row.get("event_ts")
            if abs(delta) <= 1e-12:
                continue
            if price is None or price <= 0.0:
                qty += delta
                if abs(qty) <= 1e-12:
                    qty = 0.0
                    avg_px = None
                continue
            if abs(qty) <= 1e-12:
                qty = delta
                avg_px = price
                continue
            same_direction = (qty > 0 and delta > 0) or (qty < 0 and delta < 0)
            if same_direction:
                new_abs_qty = abs(qty) + abs(delta)
                avg_px = ((abs(qty) * float(avg_px or price)) + (abs(delta) * price)) / new_abs_qty
                qty += delta
                continue

            existing_sign = 1.0 if qty > 0 else -1.0
            close_qty = min(abs(qty), abs(delta))
            realized_pnl += close_qty * (price - float(avg_px or price)) * existing_sign
            new_qty = qty + delta
            if abs(new_qty) <= 1e-12:
                qty = 0.0
                avg_px = None
            elif (qty > 0 and new_qty > 0) or (qty < 0 and new_qty < 0):
                qty = new_qty
            else:
                qty = new_qty
                avg_px = price
        return {
            "qty": float(qty),
            "avg_px": float(avg_px) if avg_px is not None and abs(qty) > 1e-12 else None,
            "realized_pnl": float(realized_pnl),
            "commission_sum": float(commission_sum),
            "fees_sum": float(fees_sum),
            "last_event_ts": last_event_ts,
        }

    async def append_fill_event(
        self,
        *,
        strategy_id: str,
        strategy_instance_id: Any = None,
        symbol: str,
        side: str,
        qty: Any,
        price: Any,
        event_ts: datetime,
        commission: Any = 0.0,
        broker_order_id: Any = None,
        ib_perm_id: Any = None,
        exec_id: Any = None,
        idempotency_key: Any = None,
        order_ref: Any = None,
        lot_id: Any = None,
        position_owner_key: Any = None,
        bracket_id: Any = None,
        order_role: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "safety_kernel_fill",
    ) -> bool:
        if self.pg is None:
            return False
        if not self._account_id_is_known():
            logger.warning(
                "position_ledger_fill_event_blocked_unknown_account",
                strategy_id=str(strategy_id or "").strip(),
                symbol=str(symbol or "").strip().upper(),
            )
            return False
        side_u = str(side or "").strip().upper()
        if side_u not in {"BUY", "SELL"}:
            return False
        try:
            qty_f = abs(float(qty or 0.0))
            price_f = float(price or 0.0)
        except Exception:
            return False
        if qty_f <= 0.0 or price_f <= 0.0:
            return False
        qty_delta = qty_f if side_u == "BUY" else -qty_f
        broker_order_id_i = self._optional_int(broker_order_id)
        ib_perm_id_i = self._optional_int(ib_perm_id)
        normalized_instance = self._normalize_owner_text(strategy_instance_id)
        normalized_lot = self._normalize_lot_id(lot_id)
        owner_key = str(position_owner_key or "").strip() or self._position_owner_key(
            strategy_id=strategy_id,
            strategy_instance_id=normalized_instance,
            symbol=symbol,
            lot_id=normalized_lot,
        )
        payload = json.dumps(dict(metadata or {}), ensure_ascii=False)
        row = await self.pg.fetchone(
            """
            INSERT INTO core.position_ledger_event (
                ledger_event_id, account_id, strategy_id, symbol, lot_id,
                event_type, event_ts, side, qty_delta, price,
                commission, fees, currency, broker_order_id, ib_perm_id,
                exec_id, idempotency_key, order_ref, source, metadata,
                strategy_instance_id, position_owner_key, bracket_id, order_role,
                created_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4,
                'FILL', $5, $6, $7, $8,
                $9, 0, 'USD', $10, $11,
                $12, $13, $14, $15, $16::jsonb,
                $17, $18, $19, $20,
                now()
            )
            ON CONFLICT DO NOTHING
            RETURNING ledger_event_id
            """,
            self.account_id,
            str(strategy_id or "").strip(),
            str(symbol or "").strip().upper(),
            normalized_lot,
            event_ts,
            side_u,
            qty_delta,
            price_f,
            float(commission or 0.0),
            broker_order_id_i,
            ib_perm_id_i,
            str(exec_id or "").strip() or None,
            str(idempotency_key or "").strip() or None,
            str(order_ref or "").strip() or None,
            str(source or "safety_kernel_fill"),
            payload,
            normalized_instance,
            owner_key,
            str(bracket_id or "").strip() or None,
            str(order_role or "").strip().upper() or None,
        )
        inserted = bool(row)
        if inserted:
            await self._refresh_current_state_for_lane(
                strategy_id=str(strategy_id or "").strip(),
                strategy_instance_id=normalized_instance,
                symbol=str(symbol or "").strip().upper(),
                lot_id=normalized_lot,
            )
            logger.info(
                "position_ledger_fill_event_appended",
                strategy_id=str(strategy_id or ""),
                symbol=str(symbol or ""),
                exec_id=str(exec_id or ""),
            )
        return inserted

    async def append_adjustment_event(
        self,
        *,
        strategy_id: str,
        strategy_instance_id: Any = None,
        symbol: str,
        qty_delta: Any,
        event_ts: datetime,
        price: Any = None,
        idempotency_key: Any = None,
        lot_id: Any = None,
        position_owner_key: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "broker_truth_convergence",
    ) -> bool:
        if self.pg is None:
            return False
        if not self._account_id_is_known():
            logger.warning(
                "position_ledger_adjustment_event_blocked_unknown_account",
                strategy_id=str(strategy_id or "").strip(),
                symbol=str(symbol or "").strip().upper(),
            )
            return False
        try:
            qty_delta_f = float(qty_delta or 0.0)
        except Exception:
            return False
        if abs(qty_delta_f) <= 1e-9:
            return False
        price_f: Optional[float]
        try:
            price_f = float(price) if price is not None and str(price).strip() != "" else None
        except Exception:
            price_f = None
        side = "BUY" if qty_delta_f > 0 else "SELL"
        normalized_instance = self._normalize_owner_text(strategy_instance_id)
        normalized_lot = self._normalize_lot_id(lot_id)
        payload = json.dumps(dict(metadata or {}), ensure_ascii=False)
        row = await self.pg.fetchone(
            """
            INSERT INTO core.position_ledger_event (
                ledger_event_id, account_id, strategy_id, symbol, lot_id,
                event_type, event_ts, side, qty_delta, price,
                commission, fees, currency, broker_order_id, ib_perm_id,
                exec_id, idempotency_key, order_ref, source, metadata,
                strategy_instance_id, position_owner_key,
                created_at
            ) VALUES (
                gen_random_uuid(), $1, $2, $3, $4,
                'ADJUSTMENT', $5, $6, $7, $8,
                0, 0, 'USD', NULL, NULL,
                NULL, $9, NULL, $10, $11::jsonb,
                $12, $13,
                now()
            )
            ON CONFLICT DO NOTHING
            RETURNING ledger_event_id
            """,
            self.account_id,
            str(strategy_id or "").strip(),
            str(symbol or "").strip().upper(),
            normalized_lot,
            event_ts,
            side,
            qty_delta_f,
            price_f,
            str(idempotency_key or "").strip() or None,
            str(source or "broker_truth_convergence"),
            payload,
            normalized_instance,
            str(position_owner_key or "").strip() or self._position_owner_key(
                strategy_id=strategy_id,
                strategy_instance_id=normalized_instance,
                symbol=symbol,
                lot_id=normalized_lot,
            ),
        )
        inserted = bool(row)
        if inserted:
            await self._refresh_current_state_for_lane(
                strategy_id=str(strategy_id or "").strip(),
                strategy_instance_id=normalized_instance,
                symbol=str(symbol or "").strip().upper(),
                lot_id=normalized_lot,
            )
            logger.info(
                "position_ledger_adjustment_event_appended",
                strategy_id=str(strategy_id or ""),
                symbol=str(symbol or ""),
                qty_delta=qty_delta_f,
                idempotency_key=str(idempotency_key or ""),
            )
        return inserted

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None or str(value).strip() == "":
            return None
        try:
            return int(value)
        except Exception:
            return None

    def _account_id_is_known(self) -> bool:
        value = str(self.account_id or "").strip()
        return bool(value) and value.upper() != "UNKNOWN_ACCOUNT"

    @staticmethod
    def _normalize_lot_id(value: Any) -> Optional[str]:
        return str(value).strip() if value is not None and str(value).strip() else None

    @staticmethod
    def _normalize_owner_text(value: Any) -> Optional[str]:
        return str(value).strip() if value is not None and str(value).strip() else None

    @classmethod
    def _position_owner_key(
        cls,
        *,
        strategy_id: Any,
        strategy_instance_id: Any,
        symbol: Any,
        lot_id: Any,
    ) -> Optional[str]:
        strategy = str(strategy_id or "").strip()
        instance = str(strategy_instance_id or "").strip()
        sym = str(symbol or "").strip().upper()
        lot = cls._normalize_lot_id(lot_id)
        if not (strategy and instance and sym and lot):
            return None
        return f"{strategy}:{instance}:{sym}:{lot}"
