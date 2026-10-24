"""
Capital reservation service
Based on doc/17.md - uses PostgreSQL stored procedures for atomic reservation
"""
from typing import Optional, Tuple
from dataclasses import dataclass
import uuid

from libs.db.postgres import PostgresDB
from libs.logger import get_logger

logger = get_logger(__name__)
_UUID_ZERO = "00000000-0000-0000-0000-000000000000"


@dataclass
class ReservationResult:
    """Result from reservation attempt"""
    approved: bool
    reserved_usd: float
    reason: str
    reservation_id: Optional[str] = None


class ReservationService:
    """
    Capital reservation service with advisory locks

    Uses PostgreSQL stored procedures for:
    - Atomic approve & reserve with strategy×symbol locking
    - Terminal state release
    - Stale reservation cleanup
    """

    def __init__(self, db: PostgresDB):
        """
        Initialize reservation service

        Args:
            db: PostgreSQL database connection
        """
        self.db = db
        logger.info("ReservationService initialized")

    async def approve_and_reserve(
        self,
        strategy_id: str,
        symbol: str,
        notional_usd: float,
        buffer_bps: Optional[int],
        idempotency_key: str
    ) -> ReservationResult:
        """
        Approve and reserve capital for an order

        This calls core_sp_risk_approve_and_reserve stored procedure which:
        1. Checks idempotency (returns existing if duplicate)
        2. Acquires advisory lock on strategy×symbol
        3. Checks capital limits
        4. Creates reservation and updates ledger atomically

        Args:
            strategy_id: Strategy ID
            symbol: Symbol
            notional_usd: Notional amount in USD
            buffer_bps: Buffer in basis points (None = use strategy default)
            idempotency_key: Unique key for idempotency

        Returns:
            ReservationResult with approval status
        """
        try:
            # Call stored procedure (async to avoid blocking event loop)
            # CRITICAL: asyncpg uses $1, $2, ... for parameters, NOT %s
            result = await self.db.fetchone(
                "SELECT * FROM core_sp_risk_approve_and_reserve($1, $2, $3, $4, $5)",
                strategy_id, symbol, notional_usd, buffer_bps, idempotency_key
            )

            if not result:
                return ReservationResult(
                    approved=False,
                    reserved_usd=0.0,
                    reason="PROCEDURE_FAILED"
                )

            approved = result.get('approved', False)
            reserved = float(result.get('reserved_usd', 0))
            reason = result.get('reason', '')

            logger.info(
                "Reservation result",
                strategy_id=strategy_id,
                symbol=symbol,
                approved=approved,
                reserved=reserved,
                reason=reason
            )

            return ReservationResult(
                approved=approved,
                reserved_usd=reserved,
                reason=reason
            )

        except Exception as e:
            logger.error("Reservation error", error=str(e), strategy_id=strategy_id, symbol=symbol)
            return ReservationResult(
                approved=False,
                reserved_usd=0.0,
                reason=f"ERROR: {str(e)}"
            )

    async def release(self, idempotency_key: str) -> bool:
        """
        Simple release method for compatibility with RiskService

        Args:
            idempotency_key: Reservation idempotency key

        Returns:
            True if release succeeded
        """
        # Call release_on_terminal with default values
        success, _ = await self.release_on_terminal(
            idempotency_key=idempotency_key,
            order_id=_UUID_ZERO,
            terminal_state="CANCELLED",
            reason="Manual release"
        )
        return success

    async def release_on_terminal(
        self,
        idempotency_key: str,
        order_id: str,
        terminal_state: str,
        reason: Optional[str] = None
    ) -> Tuple[bool, float]:
        """
        Release reservation when order reaches terminal state

        Calls core_sp_release_on_terminal stored procedure

        Args:
            idempotency_key: Original idempotency key
            order_id: Order ID (UUID)
            terminal_state: Terminal state (FILLED, CANCELLED, REJECTED)
            reason: Optional reason

        Returns:
            Tuple of (success, released_amount)
        """
        # Defensive: stored proc expects UUID. Some callers pass broker order ids for observability.
        try:
            uuid.UUID(str(order_id))
        except Exception:
            order_id = _UUID_ZERO
        try:
            result = await self.db.fetchone(
                "SELECT * FROM core_sp_release_on_terminal($1, $2, $3, $4)",
                idempotency_key, order_id, terminal_state, reason
            )

            if not result:
                return (False, 0.0)

            released = result.get('released', False)
            amount = float(result.get('released_usd', 0))

            logger.info(
                "Released reservation",
                idempotency_key=idempotency_key,
                order_id=order_id,
                terminal_state=terminal_state,
                released=released,
                amount=amount
            )

            return (released, amount)

        except Exception as e:
            logger.error("Release error", error=str(e), idempotency_key=idempotency_key)
            return (False, 0.0)

    def sweep_stale_reservations(self, cutoff_minutes: int = 30) -> int:
        """
        Clean up stale reservations that haven't reached terminal state

        Calls core_sp_sweep_stale_reservations stored procedure

        Args:
            cutoff_minutes: Age threshold in minutes

        Returns:
            Number of reservations swept
        """
        try:
            result = self.db.fetchone(
                "SELECT * FROM core_sp_sweep_stale_reservations(%s)",
                (cutoff_minutes,)
            )

            swept = int(result.get('swept', 0)) if result else 0
            logger.info("Swept stale reservations", count=swept, cutoff_minutes=cutoff_minutes)
            return swept

        except Exception as e:
            logger.error("Sweep error", error=str(e))
            return 0

    def get_strategy_ledger(self, strategy_id: str) -> Optional[dict]:
        """
        Get current ledger for strategy

        Args:
            strategy_id: Strategy ID

        Returns:
            Ledger dict or None
        """
        try:
            result = self.db.fetchone(
                """
                SELECT strategy_id, reserved_usd, realized_pnl_usd, updated_at, version
                FROM core_strategy_ledger
                WHERE strategy_id = %s
                """,
                (strategy_id,)
            )
            return result

        except Exception as e:
            logger.error("Get ledger error", error=str(e), strategy_id=strategy_id)
            return None

    def get_available_capital(self, strategy_id: str) -> float:
        """
        Calculate available capital for strategy

        Args:
            strategy_id: Strategy ID

        Returns:
            Available capital in USD
        """
        try:
            result = self.db.fetchone(
                """
                SELECT s.capital_limit_usd - l.reserved_usd AS available
                FROM core_strategy s
                JOIN core_strategy_ledger l USING(strategy_id)
                WHERE s.strategy_id = %s
                """,
                (strategy_id,)
            )

            return float(result.get('available', 0)) if result else 0.0

        except Exception as e:
            logger.error("Get available capital error", error=str(e), strategy_id=strategy_id)
            return 0.0
