"""
Order routing shim for hedging.

Provides a thin adapter over SafetyKernel so HedgeExecutor can submit
simple hedge orders (stocks/ETFs) and fail fast on unsupported option
routes. This keeps the hedging path from crashing when options execution
is not yet wired.
"""

from typing import Optional, Dict, Any

from libs.logger import get_logger

logger = get_logger(__name__)


class SafetyKernelOrderRouter:
    """Minimal router that uses SafetyKernel for stock/ETF orders."""

    def __init__(self, safety_kernel):
        self.kernel = safety_kernel

    async def place_option(
        self,
        occ_symbol: str,
        side: str,
        qty: int,
        limit_price: float,
        tif: str,
        strategy_id: str = "sys_hedger",
    ) -> Dict[str, Any]:
        """Options are not supported via SafetyKernel; return failure."""
        logger.warning(
            "hedge_option_route_unsupported",
            occ_symbol=occ_symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
        )
        return {"success": False, "error": "Option routing not supported via SafetyKernelOrderRouter"}

    async def place_stock_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str = "DAY",
        strategy_id: str = "sys_hedger",
    ) -> Dict[str, Any]:
        """Submit a market order via SafetyKernel."""
        try:
            order = await self.kernel.place_entry_order(
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="MKT",
                tif=tif,
            )
            if order:
                return {"success": True, "order_id": order.order_id}
            return {"success": False, "error": "Order placement returned None"}
        except Exception as exc:  # pragma: no cover - runtime safety
            logger.error("hedge_stock_route_failed", symbol=symbol, side=side, qty=qty, error=str(exc), exc_info=True)
            return {"success": False, "error": str(exc)}
