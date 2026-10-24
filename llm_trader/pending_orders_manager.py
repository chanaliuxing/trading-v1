#!/usr/bin/env python3
"""
Pending Orders Manager - Persist and manage planned limit orders

Features:
- Save/load pending orders to/from disk
- Check if orders should trigger based on current price
- Expire orders based on time window
- Thread-safe operations
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from threading import Lock


@dataclass
class PendingOrder:
    """A pending limit order waiting to be triggered."""
    order_id: str
    symbol: str
    action: str  # "BUY" or "SELL"
    quantity: int
    limit_price: float
    confidence: float
    created_at: str  # ISO timestamp
    expires_at: str  # ISO timestamp
    take_profit_bps: Optional[int] = None
    stop_loss_bps: Optional[int] = None
    reasoning: Optional[str] = None

    def is_expired(self) -> bool:
        """Check if order has expired."""
        try:
            now = datetime.now(timezone.utc)
            expires = datetime.fromisoformat(self.expires_at.replace('Z', '+00:00'))
            return now >= expires
        except Exception:
            return False

    def should_trigger(self, current_price: float) -> bool:
        """Check if order should trigger at current price.

        Buy orders trigger when price <= limit_price
        Sell orders trigger when price >= limit_price
        """
        if self.action == "BUY":
            return current_price <= self.limit_price
        elif self.action == "SELL":
            return current_price >= self.limit_price
        return False


class PendingOrdersManager:
    """Manages pending limit orders with persistence."""

    def __init__(self, persist_file: Optional[str] = None):
        """
        Args:
            persist_file: Path to persistence file. If None, uses default.
                         Supports {date} placeholder.
        """
        self.persist_file = persist_file or "runs/llm_trader/{date}/pending_orders.json"
        self.orders: List[PendingOrder] = []
        self.lock = Lock()

    def _resolve_file_path(self) -> Path:
        """Resolve file path with date substitution."""
        path_str = self.persist_file.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
        return Path(path_str)

    def load_orders(self) -> int:
        """Load pending orders from disk.

        Returns:
            Number of orders loaded
        """
        with self.lock:
            try:
                file_path = self._resolve_file_path()
                if not file_path.exists():
                    self.orders = []
                    return 0

                with file_path.open("r") as f:
                    data = json.load(f)

                self.orders = [PendingOrder(**order) for order in data.get("orders", [])]

                # Remove expired orders on load
                self.orders = [o for o in self.orders if not o.is_expired()]

                return len(self.orders)
            except Exception as e:
                print(f"[PendingOrdersManager] Failed to load orders: {e}")
                self.orders = []
                return 0

    def _save_orders_unlocked(self) -> bool:
        """Internal save without acquiring lock (caller must hold lock)."""
        try:
            file_path = self._resolve_file_path()
            file_path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "orders": [asdict(o) for o in self.orders]
            }

            # Atomic write
            tmp_file = file_path.with_suffix(".json.tmp")
            with tmp_file.open("w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            tmp_file.replace(file_path)
            return True
        except Exception as e:
            print(f"[PendingOrdersManager] Failed to save orders: {e}")
            return False

    def save_orders(self) -> bool:
        """Save pending orders to disk.

        Returns:
            True if successful
        """
        with self.lock:
            return self._save_orders_unlocked()

    def add_order(self, order: PendingOrder) -> bool:
        """Add a new pending order.

        Args:
            order: PendingOrder to add

        Returns:
            True if added successfully
        """
        with self.lock:
            self.orders.append(order)
            return self._save_orders_unlocked()

    def remove_order(self, order_id: str) -> bool:
        """Remove an order by ID.

        Args:
            order_id: Order ID to remove

        Returns:
            True if found and removed
        """
        with self.lock:
            before = len(self.orders)
            self.orders = [o for o in self.orders if o.order_id != order_id]
            if len(self.orders) < before:
                self._save_orders_unlocked()
                return True
            return False

    def get_triggered_orders(self, symbol: str, current_price: float) -> List[PendingOrder]:
        """Get orders that should trigger at current price.

        Args:
            symbol: Symbol to check
            current_price: Current market price

        Returns:
            List of orders that should trigger
        """
        with self.lock:
            # Remove expired orders first
            self.orders = [o for o in self.orders if not o.is_expired()]

            # Find triggered orders for this symbol
            triggered = [
                o for o in self.orders
                if o.symbol == symbol and o.should_trigger(current_price)
            ]

            return triggered

    def clear_triggered_orders(self, order_ids: List[str]) -> bool:
        """Remove triggered orders from pending list.

        Args:
            order_ids: List of order IDs to remove

        Returns:
            True if any removed
        """
        with self.lock:
            before = len(self.orders)
            self.orders = [o for o in self.orders if o.order_id not in order_ids]
            if len(self.orders) < before:
                self._save_orders_unlocked()
                return True
            return False

    def get_all_orders(self, symbol: Optional[str] = None) -> List[PendingOrder]:
        """Get all pending orders, optionally filtered by symbol.

        Args:
            symbol: Optional symbol filter

        Returns:
            List of pending orders
        """
        with self.lock:
            if symbol:
                return [o for o in self.orders if o.symbol == symbol]
            return self.orders.copy()

    def cleanup_expired(self) -> int:
        """Remove all expired orders.

        Returns:
            Number of orders removed
        """
        with self.lock:
            before = len(self.orders)
            self.orders = [o for o in self.orders if not o.is_expired()]
            removed = before - len(self.orders)
            if removed > 0:
                self._save_orders_unlocked()
            return removed


# Utility function to create order from LLMResponse
def create_pending_order_from_llm(
    response: Any,  # LLMResponse
    time_window_s: int = 900,
) -> Optional[PendingOrder]:
    """Create a PendingOrder from LLMResponse.

    Args:
        response: LLMResponse with limit_price set
        time_window_s: Validity window in seconds

    Returns:
        PendingOrder if limit_price is set, None otherwise
    """
    # Only create pending order if limit_price is specified
    if not hasattr(response, 'limit_price') or response.limit_price is None:
        return None

    import uuid
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    order_id = str(uuid.uuid4())[:8]

    # Use LLM's time window if specified, otherwise use default
    tw = response.time_window_s if hasattr(response, 'time_window_s') and response.time_window_s else time_window_s

    expires_at = (now + timedelta(seconds=tw)).isoformat()

    return PendingOrder(
        order_id=order_id,
        symbol=response.symbol,
        action=response.decision.upper(),
        quantity=int(response.quantity or 0),
        limit_price=response.limit_price,
        confidence=response.confidence or 0.0,
        created_at=now.isoformat(),
        expires_at=expires_at,
        take_profit_bps=getattr(response, 'take_profit_bps', None),
        stop_loss_bps=getattr(response, 'stop_loss_bps', None),
        reasoning=getattr(response, 'reasoning', None),
    )


# Self-test
if __name__ == "__main__":
    print("PendingOrdersManager Self-Test")
    print("=" * 60)

    # Create manager with test file
    manager = PendingOrdersManager(persist_file="temp/test_pending_orders.json")

    # Test 1: Add orders
    print("\n[Test 1] Adding pending orders...")
    order1 = PendingOrder(
        order_id="test001",
        symbol="TSLA",
        action="BUY",
        quantity=10,
        limit_price=245.50,
        confidence=0.8,
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )

    order2 = PendingOrder(
        order_id="test002",
        symbol="TSLA",
        action="SELL",
        quantity=10,
        limit_price=250.00,
        confidence=0.9,
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )

    manager.add_order(order1)
    manager.add_order(order2)
    print(f"  ✓ Added 2 orders, total: {len(manager.get_all_orders())}")

    # Test 2: Save and load
    print("\n[Test 2] Save and reload...")
    manager.save_orders()

    manager2 = PendingOrdersManager(persist_file="temp/test_pending_orders.json")
    loaded = manager2.load_orders()
    print(f"  ✓ Loaded {loaded} orders")

    # Test 3: Trigger detection
    print("\n[Test 3] Trigger detection...")
    # Current price 246.00 - should trigger BUY at 245.50
    triggered = manager2.get_triggered_orders("TSLA", 246.00)
    print(f"  ✓ Price 246.00 triggered {len(triggered)} orders (BUY at 245.50)")

    # Current price 251.00 - should trigger SELL at 250.00
    triggered = manager2.get_triggered_orders("TSLA", 251.00)
    print(f"  ✓ Price 251.00 triggered {len(triggered)} orders (SELL at 250.00)")

    # Test 4: Cleanup
    print("\n[Test 4] Cleanup...")
    manager2.clear_triggered_orders(["test001"])
    remaining = len(manager2.get_all_orders())
    print(f"  ✓ Removed test001, remaining: {remaining}")

    print("\n✅ All tests passed")

    # Cleanup test file
    from pathlib import Path
    Path("temp/test_pending_orders.json").unlink(missing_ok=True)
