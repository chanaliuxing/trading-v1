# broker/adapter.py
from __future__ import annotations
from typing import Dict, Any, List


class BrokerAdapter:
    """Base class for broker adapters."""

    def place_order(self, symbol: str, side: str, qty: int, order_type: str="MKT", limit_price: float|None=None) -> Dict[str,Any]:
        """
        Place order with broker.

        Args:
            symbol: Trading symbol
            side: "BUY" or "SELL"
            qty: Quantity
            order_type: "MKT" or "LMT"
            limit_price: Limit price (for LMT orders)

        Returns:
            Response dict with ok status and order_id
        """
        raise NotImplementedError

    def fetch_fills(self) -> List[Dict[str,Any]]:
        """
        Fetch recent fills from broker.

        Returns:
            List of fill dicts with ts_ms, symbol, side, qty, price, order_id
        """
        raise NotImplementedError


class RealBrokerStub(BrokerAdapter):
    """
    Stub implementation for real broker integration.

    Replace with actual broker SDK (IBKR/Alpaca/Tradier) for production.
    """
    def __init__(self, **kwargs):
        pass

    def place_order(self, symbol, side, qty, order_type="MKT", limit_price=None):
        """Stub implementation - returns mock success."""
        return {
            "ok": True,
            "order_id": f"stub-{symbol}-{side}-{qty}"
        }

    def fetch_fills(self):
        """Stub implementation - returns empty list."""
        return []  # Replace with actual SDK call
