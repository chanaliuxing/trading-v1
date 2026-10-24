from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from libs.logger import get_logger

from .fill_allocator import FillAllocator
from .models import ChildOrderState

logger = get_logger(__name__)


class NettingFillFanout:
    """
    Fan out a parent netted fill into child strategy orders.
    """

    def __init__(self, *, pg, reservation_service: Optional[Any] = None):
        self.pg = pg
        self.reservation = reservation_service
        self.allocator = FillAllocator()

    async def on_parent_fill(
        self,
        *,
        parent_order_uuid: Any,
        parent_fill: Any,
        parent_filled_qty: float,
    ) -> None:
        parent_uuid_s = str(parent_order_uuid)
        try:
            child_rows = await self.pg.fetchall(
                """
                SELECT order_id, strategy_id, decision_id,
                       idempotency_key, qty, state, created_at,
                       COALESCE(filled.filled_qty, 0) AS filled_qty
                FROM (
                    SELECT o.order_id, o.strategy_id,
                           COALESCE(
                               substring(COALESCE(o.note, '') FROM '"decision_id"\\s*:\\s*"([^"]+)"'),
                               ''
                           ) AS decision_id,
                           o.idempotency_key, o.qty, o.state, o.created_at
                    FROM core.core_order o
                    WHERE o.parent_order_id = $1::uuid
                ) base
                LEFT JOIN (
                    SELECT order_id, COALESCE(SUM(qty), 0) AS filled_qty
                    FROM core.core_fill
                    GROUP BY order_id
                ) filled ON filled.order_id = base.order_id
                ORDER BY base.created_at ASC
                """,
                parent_uuid_s,
            )
        except Exception as exc:
            logger.warning("netting_fanout_children_query_failed", parent_order_id=parent_uuid_s, error=str(exc))
            return

        if not child_rows:
            return

        try:
            row = await self.pg.fetchone(
                """
                SELECT COALESCE(SUM(f.qty), 0) AS allocated_qty
                FROM core.core_fill f
                JOIN core.core_order o ON o.order_id = f.order_id
                WHERE o.parent_order_id = $1::uuid
                """,
                parent_uuid_s,
            )
            allocated_qty = float((row or {}).get("allocated_qty") or 0.0)
        except Exception:
            allocated_qty = 0.0

        delta = float(parent_filled_qty) - allocated_qty
        if delta <= 1e-9:
            return

        children: List[ChildOrderState] = []
        row_by_id: Dict[str, Dict[str, Any]] = {}
        for r in child_rows:
            oid = str(r.get("order_id"))
            qty = float(r.get("qty") or 0.0)
            filled = float(r.get("filled_qty") or 0.0)
            remaining = max(0.0, qty - filled)
            children.append(
                ChildOrderState(
                    child_order_id=oid,
                    strategy_id=str(r.get("strategy_id") or ""),
                    decision_id=str(r.get("decision_id") or ""),
                    remaining_qty=remaining,
                )
            )
            row_by_id[oid] = dict(r)

        allocations, leftover = self.allocator.allocate(fill_qty=delta, fill_price=float(parent_fill.price), children=children)
        if not allocations:
            return

        ts = datetime.fromtimestamp(parent_fill.ts_us / 1e6)
        for idx, alloc in enumerate(allocations):
            child_order_id = alloc.child_order_id
            exec_id = f"{parent_fill.exec_id}:{child_order_id}:{idx}"
            row = row_by_id.get(child_order_id, {})
            try:
                await self.pg.execute(
                    """
                    INSERT INTO core.core_fill (
                        fill_id, order_id, ts, qty, price,
                        liquidity, commission_usd, exchange, exec_id, created_at
                    ) VALUES (
                        gen_random_uuid(), $1::uuid, $2, $3, $4,
                        $5, $6, $7, $8, now()
                    )
                    ON CONFLICT (order_id, ts, qty, price) DO NOTHING
                    """,
                    child_order_id,
                    ts,
                    float(alloc.qty),
                    float(alloc.price),
                    getattr(parent_fill, "liquidity", None),
                    0.0,
                    getattr(parent_fill, "exchange", None),
                    exec_id,
                )
            except Exception as exc:
                logger.warning(
                    "netting_fanout_insert_fill_failed",
                    parent_order_id=parent_uuid_s,
                    child_order_id=child_order_id,
                    error=str(exc),
                )
                continue

            await self._refresh_child_state(child_order_id, order_qty=float(row.get("qty") or 0.0), idempotency_key=str(row.get("idempotency_key") or ""))

        if leftover > 1e-9:
            logger.warning("netting_fanout_leftover", parent_order_id=parent_uuid_s, leftover_qty=leftover)

    async def _refresh_child_state(self, child_order_id: str, *, order_qty: float, idempotency_key: str) -> None:
        try:
            row = await self.pg.fetchone(
                """
                SELECT COALESCE(SUM(qty), 0) AS filled_qty
                FROM core.core_fill
                WHERE order_id = $1::uuid
                """,
                child_order_id,
            )
            filled = float((row or {}).get("filled_qty") or 0.0)
        except Exception:
            filled = 0.0

        state = "FILLED" if (order_qty > 0 and filled >= (order_qty - 1e-9)) else ("PARTIAL" if filled > 0 else "SUBMITTED")
        try:
            await self.pg.execute(
                """
                UPDATE core.core_order
                SET state = $1,
                    updated_at = now()
                WHERE order_id = $2::uuid
                """,
                state,
                child_order_id,
            )
        except Exception as exc:
            logger.warning("netting_fanout_update_state_failed", child_order_id=child_order_id, state=state, error=str(exc))
            return

        if state == "FILLED" and idempotency_key and self.reservation is not None:
            try:
                await self.reservation.release_on_terminal(
                    idempotency_key=idempotency_key,
                    order_id=child_order_id,
                    terminal_state="FILLED",
                    reason="Netting child order FILLED",
                )
            except Exception as exc:
                logger.warning(
                    "netting_fanout_release_failed",
                    child_order_id=child_order_id,
                    idempotency_key=idempotency_key,
                    error=str(exc),
                )
