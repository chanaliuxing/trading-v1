"""
Trading Mode Manager

Supports three trading modes:
1. SHADOW: Generate decisions but don't submit to broker (dry-run)
2. PAPER: Submit to IB paper trading account
3. LIVE: Submit to IB live trading account

Based on PRODUCTION_READINESS.md requirements.
"""
import os
import time
import uuid
from enum import Enum
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

import asyncpg

from libs.logger import get_logger

logger = get_logger(__name__)


class TradingMode(Enum):
    """Trading mode enum"""
    SHADOW = "shadow"  # Dry-run, no broker submission
    PAPER = "paper"    # IB paper trading
    LIVE = "live"      # IB live trading


@dataclass
class ShadowOrder:
    """Shadow order record"""
    shadow_id: str
    decision_id: str
    strategy_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    limit_px: Optional[float]
    timestamp: str
    would_submit: bool
    reason: Optional[str]


class TradingModeManager:
    """
    Trading Mode Manager

    Controls whether decisions are executed, shadow-logged, or paper-traded.
    """

    def __init__(
        self,
        mode: TradingMode,
        db_pool: asyncpg.Pool,
        shadow_symbols: Optional[List[str]] = None
    ):
        """
        Initialize trading mode manager

        Args:
            mode: Trading mode (SHADOW/PAPER/LIVE)
            db_pool: PostgreSQL connection pool
            shadow_symbols: Optional list of symbols for shadow mode
        """
        self.mode = mode
        self.db = db_pool
        self.shadow_symbols = shadow_symbols or []

        logger.info(
            "Trading mode initialized",
            mode=mode.value,
            shadow_symbols=shadow_symbols
        )

    async def process_decision(
        self,
        decision_envelope: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process decision based on trading mode

        Args:
            decision_envelope: Decision envelope from strategy

        Returns:
            Processing result with action taken
        """
        symbol = decision_envelope["symbol"]
        decision_id = decision_envelope["decision_id"]

        if self.mode == TradingMode.SHADOW:
            # Shadow mode: Log decision but don't execute
            return await self._process_shadow(decision_envelope)

        elif self.mode == TradingMode.PAPER:
            # Paper trading: Submit to IB paper account
            return await self._process_paper(decision_envelope)

        elif self.mode == TradingMode.LIVE:
            # Live trading: Submit to IB live account
            # Additional safety checks for live mode
            if not await self._pre_live_checks(decision_envelope):
                logger.error(
                    "Pre-live check failed, blocking order",
                    decision_id=decision_id,
                    symbol=symbol
                )
                return {
                    "status": "blocked",
                    "reason": "pre_live_check_failed",
                    "decision_id": decision_id
                }

            return await self._process_live(decision_envelope)

    async def _process_shadow(
        self,
        decision_envelope: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process decision in shadow mode (dry-run)

        Args:
            decision_envelope: Decision envelope

        Returns:
            Shadow processing result
        """
        symbol = decision_envelope["symbol"]
        decision_id = decision_envelope["decision_id"]

        # Check if symbol is in shadow list (if specified)
        if self.shadow_symbols and symbol not in self.shadow_symbols:
            logger.debug(
                "Symbol not in shadow list, skipping",
                symbol=symbol,
                shadow_symbols=self.shadow_symbols
            )
            return {
                "status": "skipped",
                "reason": "not_in_shadow_list",
                "decision_id": decision_id
            }

        # Create shadow order
        shadow_order = ShadowOrder(
            shadow_id=str(uuid.uuid4()),
            decision_id=decision_id,
            strategy_id=decision_envelope["strategy_id"],
            symbol=symbol,
            side=self._get_side_from_qty(decision_envelope["target_qty"]),
            qty=abs(int(decision_envelope["target_qty"])),
            order_type=decision_envelope["order_style"]["kind"],
            limit_px=decision_envelope["order_style"].get("limit_px"),
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            would_submit=True,
            reason="shadow_mode"
        )

        # Log to shadow_orders table
        await self._store_shadow_order(shadow_order)

        logger.info(
            "Shadow order logged",
            shadow_id=shadow_order.shadow_id,
            symbol=symbol,
            qty=shadow_order.qty,
            side=shadow_order.side
        )

        return {
            "status": "shadow_logged",
            "shadow_id": shadow_order.shadow_id,
            "decision_id": decision_id
        }

    async def _process_paper(
        self,
        decision_envelope: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process decision in paper trading mode

        Args:
            decision_envelope: Decision envelope

        Returns:
            Paper trading result
        """
        # Forward to execution service with paper account flag
        logger.info(
            "Submitting to paper account",
            decision_id=decision_envelope["decision_id"],
            symbol=decision_envelope["symbol"]
        )

        # Mark as paper trading
        decision_envelope["_trading_mode"] = self.mode.value

        return {
            "status": "submitted_paper",
            "decision_id": decision_envelope["decision_id"]
        }

    async def _process_live(
        self,
        decision_envelope: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process decision in live trading mode

        Args:
            decision_envelope: Decision envelope

        Returns:
            Live trading result
        """
        # Forward to execution service with live account flag
        logger.info(
            "Submitting to LIVE account",
            decision_id=decision_envelope["decision_id"],
            symbol=decision_envelope["symbol"]
        )

        # Mark as live trading
        decision_envelope["_trading_mode"] = self.mode.value

        return {
            "status": "submitted_live",
            "decision_id": decision_envelope["decision_id"]
        }

    async def _pre_live_checks(self, decision_envelope: Dict[str, Any]) -> bool:
        """
        Pre-live safety checks

        Args:
            decision_envelope: Decision envelope

        Returns:
            True if all checks pass
        """
        symbol = decision_envelope["symbol"]

        # Check 1: Symbol is approved for live trading
        if not await self._is_symbol_approved_for_live(symbol):
            logger.error("Symbol not approved for live trading", symbol=symbol)
            return False

        # Check 2: Notional size within limits
        notional = abs(decision_envelope["target_qty"]) * decision_envelope["extras"]["ref_px"]
        if notional > 100_000:  # $100k max per order
            logger.error("Notional exceeds live limit", symbol=symbol, notional=notional)
            return False

        # Check 3: Market hours (if required)
        # TODO: Add market hours check

        return True

    async def _is_symbol_approved_for_live(self, symbol: str) -> bool:
        """Check if symbol is approved for live trading"""
        query = """
            SELECT approved_for_live
            FROM symbol_config
            WHERE symbol = $1
        """
        row = await self.db.fetchrow(query, symbol)
        return row and row["approved_for_live"] if row else False

    def _get_side_from_qty(self, qty: float) -> str:
        """Get side from quantity"""
        if qty > 0:
            return "BUY"
        elif qty < 0:
            return "SELL"
        else:
            return "FLAT"

    async def _store_shadow_order(self, shadow_order: ShadowOrder):
        """Store shadow order in database"""
        query = """
            INSERT INTO shadow_orders (
                shadow_id,
                decision_id,
                strategy_id,
                symbol,
                side,
                qty,
                order_type,
                limit_px,
                timestamp,
                would_submit,
                reason
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """

        await self.db.execute(
            query,
            shadow_order.shadow_id,
            shadow_order.decision_id,
            shadow_order.strategy_id,
            shadow_order.symbol,
            shadow_order.side,
            shadow_order.qty,
            shadow_order.order_type,
            shadow_order.limit_px,
            shadow_order.timestamp,
            shadow_order.would_submit,
            shadow_order.reason
        )

    async def get_shadow_report(self, date_str: str) -> Dict[str, Any]:
        """
        Generate shadow trading report

        Args:
            date_str: Date in YYYY-MM-DD format

        Returns:
            Shadow trading report
        """
        query = """
            SELECT
                symbol,
                COUNT(*) as decision_count,
                SUM(qty) as total_qty,
                AVG(limit_px) as avg_px
            FROM shadow_orders
            WHERE date(timestamp) = $1
            GROUP BY symbol
            ORDER BY decision_count DESC
        """

        rows = await self.db.fetchall(query, date_str)

        report = {
            "date": date_str,
            "total_decisions": sum(r["decision_count"] for r in rows),
            "symbols": [
                {
                    "symbol": r["symbol"],
                    "decisions": r["decision_count"],
                    "total_qty": r["total_qty"],
                    "avg_px": float(r["avg_px"]) if r["avg_px"] else 0.0
                }
                for r in rows
            ]
        }

        return report


def get_trading_mode() -> TradingMode:
    """
    Get trading mode from environment

    Returns:
        TradingMode enum
    """
    mode_str = os.getenv("TRADING_MODE", "live").lower()

    if mode_str == "live":
        return TradingMode.LIVE
    elif mode_str == "paper":
        return TradingMode.PAPER
    else:
        return TradingMode.SHADOW


def get_shadow_symbols() -> List[str]:
    """
    Get shadow symbols from environment

    Returns:
        List of symbols for shadow testing
    """
    symbols_str = os.getenv("SHADOW_SYMBOLS", "")
    if symbols_str:
        return [s.strip() for s in symbols_str.split(",")]
    return []


# Example usage
async def example_usage():
    """Example: Using trading mode manager"""
    import asyncpg

    # Connect to database
    db = await asyncpg.create_pool(
        host="localhost",
        port=5432,
        user="postgres",
        password="postgres",
        database="test"
    )

    # Get trading mode from environment
    mode = get_trading_mode()
    shadow_symbols = get_shadow_symbols() if mode == TradingMode.SHADOW else None

    # Initialize manager
    manager = TradingModeManager(
        mode=mode,
        db_pool=db,
        shadow_symbols=shadow_symbols
    )

    # Process decision
    decision = {
        "decision_id": str(uuid.uuid4()),
        "strategy_id": "test_strategy",
        "symbol": "AAPL",
        "target_qty": 100.0,
        "order_style": {"kind": "LMT", "limit_px": 150.0},
        "extras": {"ref_px": 150.0}
    }

    result = await manager.process_decision(decision)
    print(f"Result: {result}")

    # Generate shadow report (if in shadow mode)
    if mode == TradingMode.SHADOW:
        from datetime import date
        report = await manager.get_shadow_report(date.today().isoformat())
        print(f"Shadow Report: {report}")
