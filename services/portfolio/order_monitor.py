"""
OrderMonitor Service - Phase 2 Implementation
Monitors order status changes and provides async wait_for_fill() functionality.
"""

import asyncio
from typing import Dict, Optional, Callable, Any
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from collections import defaultdict

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class OrderStatus:
    """Order status snapshot"""
    order_id: int
    status: str  # SUBMITTED/FILLED/CANCELLED/REJECTED/etc
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_price: float = 0.0
    perm_id: Optional[int] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(ET)


class OrderMonitor:
    """
    Order status monitoring service.

    Features:
    - Async wait_for_fill() to wait for order completion
    - Event-driven callbacks for status updates
    - Timeout handling
    - Integration with IBInsyncAdapter
    """

    def __init__(self):
        """Initialize OrderMonitor"""
        self._callbacks: Dict[int, asyncio.Future] = {}
        self._status_cache: Dict[int, OrderStatus] = {}
        self._running = False
        self._adapter = None
        self._exec_handle_epoch: Optional[int] = None
        self._trade_repo = None
        logger.info("OrderMonitor initialized")

    async def start(self):
        """Start the order monitor"""
        from services.ibkr.ib_connection_manager import get_ib_connection_manager
        from services.portfolio.option_trade_repo import OptionTradeRepository

        ib_cfg = {}
        try:
            from libs.config import config as CFG

            ib_cfg = CFG.ib_config
        except Exception:
            ib_cfg = {}
        ib_manager = get_ib_connection_manager()
        self._adapter = await ib_manager.get_exec_adapter(
            host=ib_cfg.get("host", "127.0.0.1"),
            port=int(ib_cfg.get("port", 4002)),
            account=None,
        )
        self._exec_handle_epoch = int(ib_manager.exec_connection_epoch())
        self._running = True
        try:
            self._trade_repo = OptionTradeRepository()
        except Exception:
            self._trade_repo = None

        # Register event callbacks with IB adapter
        if self._adapter and self._adapter.ib:
            self._adapter.ib.orderStatusEvent += self._on_order_status_event

        logger.info("OrderMonitor started")

    async def _ensure_adapter_fresh(self) -> None:
        if not self._running:
            return
        from services.ibkr.ib_connection_manager import get_ib_connection_manager

        ib_manager = get_ib_connection_manager()
        current_epoch = int(ib_manager.exec_connection_epoch())
        if self._adapter is not None and self._exec_handle_epoch is not None and self._exec_handle_epoch == current_epoch:
            return
        new_adapter = await ib_manager.get_exec_adapter(account=None)
        if self._adapter and self._adapter.ib and hasattr(self._adapter.ib, "orderStatusEvent"):
            try:
                self._adapter.ib.orderStatusEvent -= self._on_order_status_event
            except Exception:
                pass
        self._adapter = new_adapter
        self._exec_handle_epoch = current_epoch
        if self._adapter and self._adapter.ib:
            self._adapter.ib.orderStatusEvent += self._on_order_status_event
        logger.info("order_monitor_adapter_refreshed", exec_epoch=current_epoch)

    async def stop(self):
        """Stop the order monitor"""
        self._running = False

        # Cancel all pending futures
        for order_id, future in list(self._callbacks.items()):
            if not future.done():
                future.cancel()

        self._callbacks.clear()
        logger.info("OrderMonitor stopped")

    def _on_order_status_event(self, trade):
        """
        Callback for IB order status events.

        Called by ib_insync when order status changes.
        """
        try:
            order_id = trade.order.orderId
            status = trade.orderStatus.status

            logger.debug("order_status_event",
                        order_id=order_id,
                        status=status,
                        filled=trade.orderStatus.filled,
                        remaining=trade.orderStatus.remaining)

            # Trigger any waiting futures
            asyncio.create_task(self.on_order_status_update(order_id, status, trade))

        except Exception as exc:
            logger.error("order_status_event_error", error=str(exc), exc_info=True)

    async def on_order_status_update(self, order_id: int, status: str, trade=None):
        """
        Handle order status update.

        Args:
            order_id: Order ID
            status: New status
            trade: Optional Trade object from ib_insync
        """
        future = self._callbacks.get(order_id)

        # Check if this is a terminal status
        if status in ["Filled", "Cancelled", "Rejected", "Inactive", "ApiCancelled"]:
            if future and not future.done():
                order_status = OrderStatus(
                    order_id=order_id,
                    status=status,
                    filled_qty=trade.orderStatus.filled if trade else 0.0,
                    remaining_qty=trade.orderStatus.remaining if trade else 0.0,
                    avg_fill_price=trade.orderStatus.avgFillPrice if trade else 0.0,
                    perm_id=trade.order.permId if trade and hasattr(trade.order, 'permId') else None,
                )

                future.set_result(order_status)

                logger.info(
                    "order_status_resolved",
                    order_id=order_id,
                    status=status,
                    filled_qty=order_status.filled_qty,
                    avg_price=order_status.avg_fill_price,
                )

        # Best-effort DB update for any status change (requires v2 schema)
        if self._trade_repo and trade:
            try:
                await self._trade_repo.update_order_status(
                    order_id=order_id,
                    order_status=status,
                    filled_price=trade.orderStatus.avgFillPrice if hasattr(trade, "orderStatus") else None,
                    filled_qty=trade.orderStatus.filled if hasattr(trade, "orderStatus") else None,
                    reason=getattr(trade.orderStatus, "whyHeld", None) if hasattr(trade, "orderStatus") else None,
                    meta={
                        "ib_status": status,
                        "filled": getattr(trade.orderStatus, "filled", None),
                        "remaining": getattr(trade.orderStatus, "remaining", None),
                        "avgFillPrice": getattr(trade.orderStatus, "avgFillPrice", None),
                    },
                )
            except Exception as exc:
                logger.debug("order_status_db_update_failed", order_id=order_id, error=str(exc))

        # Cache latest status for get_status() fast path
        try:
            self._status_cache[order_id] = OrderStatus(
                order_id=order_id,
                status=status,
                filled_qty=trade.orderStatus.filled if trade else 0.0,
                remaining_qty=trade.orderStatus.remaining if trade else 0.0,
                avg_fill_price=trade.orderStatus.avgFillPrice if trade else 0.0,
                perm_id=trade.order.permId if trade and hasattr(trade.order, 'permId') else None,
            )
        except Exception:
            pass

    async def wait_for_fill(
        self,
        order_id: int,
        timeout_sec: int = 600
    ) -> OrderStatus:
        """
        Wait for order to reach terminal status (Filled/Cancelled/Rejected).

        Args:
            order_id: Order ID to monitor
            timeout_sec: Timeout in seconds (default 10 minutes)

        Returns:
            OrderStatus with final status

        Raises:
            asyncio.TimeoutError: If order doesn't complete within timeout
        """
        if order_id in self._callbacks:
            logger.warning("wait_for_fill_already_waiting", order_id=order_id)
            return await self._callbacks[order_id]

        # Create future for this order
        future = asyncio.Future()
        self._callbacks[order_id] = future

        logger.info("wait_for_fill_start",
                   order_id=order_id,
                   timeout_sec=timeout_sec)

        try:
            # Wait for future to be resolved (by _on_order_status_event)
            order_status = await asyncio.wait_for(future, timeout=timeout_sec)

            logger.info("wait_for_fill_complete",
                       order_id=order_id,
                       status=order_status.status)

            return order_status

        except asyncio.TimeoutError:
            logger.warning("wait_for_fill_timeout",
                          order_id=order_id,
                          timeout_sec=timeout_sec)

            # Return timeout status
            return OrderStatus(
                order_id=order_id,
                status="TIMEOUT",
            )

        finally:
            # Clean up callback
            self._callbacks.pop(order_id, None)

    async def get_status(self, order_id: int) -> Optional[OrderStatus]:
        """
        Query current order status from adapter.

        Args:
            order_id: Order ID to query

        Returns:
            OrderStatus or None if not found
        """
        if order_id in self._status_cache:
            return self._status_cache[order_id]
        await self._ensure_adapter_fresh()
        if not self._adapter:
            logger.error("get_status_no_adapter")
            return None

        try:
            # Check adapter's trades dict
            trade = self._adapter._trades.get(order_id)

            if trade:
                return OrderStatus(
                    order_id=order_id,
                    status=trade.orderStatus.status,
                    filled_qty=trade.orderStatus.filled,
                    remaining_qty=trade.orderStatus.remaining,
                    avg_fill_price=trade.orderStatus.avgFillPrice,
                    perm_id=trade.order.permId if hasattr(trade.order, 'permId') else None,
                )

            # Not found in adapter, try IB directly
            for trade in self._adapter.ib.trades():
                if trade.order.orderId == order_id:
                    return OrderStatus(
                        order_id=order_id,
                        status=trade.orderStatus.status,
                        filled_qty=trade.orderStatus.filled,
                        remaining_qty=trade.orderStatus.remaining,
                        avg_fill_price=trade.orderStatus.avgFillPrice,
                        perm_id=trade.order.permId if hasattr(trade.order, 'permId') else None,
                    )

            return None

        except Exception as exc:
            logger.error("get_status_error", order_id=order_id, error=str(exc))
            return None


# Global OrderMonitor singleton
_global_monitor: Optional[OrderMonitor] = None


def get_order_monitor() -> OrderMonitor:
    """
    Get global OrderMonitor singleton.

    Returns:
        Global OrderMonitor instance
    """
    global _global_monitor

    if _global_monitor is None:
        _global_monitor = OrderMonitor()
        logger.info("Created global OrderMonitor singleton")

    return _global_monitor


async def init_global_order_monitor() -> OrderMonitor:
    """
    Initialize and start the global OrderMonitor.

    Should be called once during system startup.

    Returns:
        Started global OrderMonitor instance
    """
    monitor = get_order_monitor()

    if not monitor._running:
        await monitor.start()
        logger.info("Global OrderMonitor started")

    return monitor
