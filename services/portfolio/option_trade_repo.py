"""Option spread trade repository for Postgres persistence.

This repository is best-effort: in minimal environments (e.g., unit tests)
Postgres dependencies may not be installed, and calls should fail gracefully.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from libs.logger import get_logger

logger = get_logger(__name__)


class OptionTradeRepository:
    """Async repository writing option spread trades into Postgres."""

    def __init__(self):
        # Lazy import so unit tests can import strategy modules without requiring asyncpg.
        from libs.db.postgres import PostgresDB

        self.db = PostgresDB(min_conn=1, max_conn=4)
        self._connected = False
        self._supports_v2_columns: Optional[bool] = None

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self.db.connect()
            self._connected = True

    async def _detect_v2_schema(self) -> bool:
        """Detect whether the table has the v2 columns (order_id/order_status/etc)."""
        if self._supports_v2_columns is not None:
            return self._supports_v2_columns
        try:
            await self._ensure_connected()
            row = await self.db.fetchone(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema='trading'
                  AND table_name='option_spread_trades'
                  AND column_name='order_id'
                LIMIT 1
                """
            )
            self._supports_v2_columns = bool(row)
        except Exception:
            self._supports_v2_columns = False
        return bool(self._supports_v2_columns)

    async def insert_trade(
        self,
        *,
        action: str,
        symbol: str,
        expiry: str,
        short_strike: float,
        long_strike: float,
        width: float,
        credit: float,
        qty: int,
        transmit: bool,
        meta: Optional[Dict[str, Any]] = None,
        order_id: Optional[int] = None,
        order_status: Optional[str] = None,
        filled_price: Optional[float] = None,
        filled_qty: Optional[float] = None,
        filled_ts: Optional[datetime] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Persist a trade/order record."""
        try:
            await self._ensure_connected()
            payload = json.dumps(meta or {}, ensure_ascii=False)
            if await self._detect_v2_schema():
                await self.db.execute(
                    """
                    INSERT INTO trading.option_spread_trades
                    (ts, action, symbol, expiry, short_strike, long_strike, width, credit, qty, transmit, meta,
                     order_id, order_status, filled_price, filled_qty, filled_ts, reason)
                    VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15, $16)
                    """,
                    action,
                    symbol,
                    expiry,
                    float(short_strike),
                    float(long_strike),
                    float(width),
                    float(credit),
                    int(qty),
                    bool(transmit),
                    payload,
                    int(order_id) if order_id is not None else None,
                    order_status,
                    float(filled_price) if filled_price is not None else None,
                    float(filled_qty) if filled_qty is not None else None,
                    filled_ts,
                    reason,
                )
            else:
                await self.db.execute(
                    """
                    INSERT INTO trading.option_spread_trades
                    (ts, action, symbol, expiry, short_strike, long_strike, width, credit, qty, transmit, meta)
                    VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    action,
                    symbol,
                    expiry,
                    float(short_strike),
                    float(long_strike),
                    float(width),
                    float(credit),
                    int(qty),
                    bool(transmit),
                    payload,
                )
        except Exception as exc:
            logger.error("option_trade_insert_failed", error=str(exc))

    async def update_order_status(
        self,
        *,
        order_id: int,
        order_status: str,
        filled_price: Optional[float] = None,
        filled_qty: Optional[float] = None,
        filled_ts: Optional[datetime] = None,
        reason: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update the latest record for an order_id with status/fill fields (best-effort)."""
        try:
            if not await self._detect_v2_schema():
                return
            await self._ensure_connected()
            payload = json.dumps(meta or {}, ensure_ascii=False) if meta is not None else None
            await self.db.execute(
                """
                UPDATE trading.option_spread_trades
                SET order_status = $2,
                    filled_price = COALESCE($3, filled_price),
                    filled_qty   = COALESCE($4, filled_qty),
                    filled_ts    = COALESCE($5, filled_ts),
                    reason       = COALESCE($6, reason),
                    meta         = COALESCE($7::jsonb, meta)
                WHERE id = (
                    SELECT id
                    FROM trading.option_spread_trades
                    WHERE order_id = $1
                    ORDER BY ts DESC
                    LIMIT 1
                )
                """,
                int(order_id),
                order_status,
                float(filled_price) if filled_price is not None else None,
                float(filled_qty) if filled_qty is not None else None,
                filled_ts,
                reason,
                payload,
            )
        except Exception as exc:
            logger.warning("option_trade_update_failed", order_id=order_id, error=str(exc))
