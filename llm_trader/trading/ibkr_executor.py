"""IBKR Trade Executor - Executes trades via Interactive Brokers API"""

import asyncio
import random
from typing import Optional, Dict, Any, Tuple
from datetime import datetime
from ib_insync import IB, Stock, MarketOrder, LimitOrder, Order, Trade
from enum import Enum


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class IBKRExecutor:
    """Executes trades via IBKR API"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,  # Paper trading port
        client_id: Optional[int] = None,  # Unique client ID (None = auto-generate)
    ):
        self.host = host
        self.port = port
        # Generate random client_id in range 1000-9999 if not provided
        self.client_id = client_id if client_id is not None else random.randint(1000, 9999)
        self.ib = IB()
        self.connected = False

    async def connect(self, max_retries: int = 5):
        """Connect to IBKR Gateway/TWS with smart retry on client_id conflict

        Args:
            max_retries: Maximum number of connection attempts (default: 5)
        """
        for attempt in range(max_retries):
            try:
                print(f"Connecting to IBKR at {self.host}:{self.port}, client_id={self.client_id} (attempt {attempt + 1}/{max_retries})...")
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
                self.connected = True
                print(f"✓ Connected to IBKR successfully with client_id={self.client_id}")
                return
            except Exception as e:
                error_msg = str(e).lower()

                # Check if it's a "client id already in use" error
                if "client id" in error_msg and ("already in use" in error_msg or "326" in str(e)):
                    old_id = self.client_id
                    # Generate a new random client_id
                    self.client_id = random.randint(1000, 9999)
                    print(f"⚠️  client_id {old_id} already in use, trying new client_id={self.client_id}")

                    # Recreate IB instance for clean retry
                    self.ib = IB()
                    await asyncio.sleep(0.5)  # Brief delay before retry
                else:
                    # Other error, fail immediately
                    print(f"✗ Failed to connect to IBKR: {e}")
                    self.connected = False
                    raise

        # Max retries exceeded
        print(f"✗ Failed to connect after {max_retries} attempts")
        self.connected = False
        raise RuntimeError(f"Unable to connect to IBKR after {max_retries} attempts")

    async def disconnect(self):
        """Disconnect from IBKR"""
        if self.connected:
            self.ib.disconnect()
            self.connected = False
            print("Disconnected from IBKR")

    async def get_account_summary(self) -> Dict[str, Any]:
        """Get account summary (cash, equity, etc.)"""
        if not self.connected:
            raise RuntimeError("Not connected to IBKR")

        account_values = self.ib.accountSummary()

        summary = {
            "cash": 0.0,
            "equity": 0.0,
            "buying_power": 0.0,
            "net_liquidation": 0.0,
        }

        for item in account_values:
            if item.tag == "TotalCashValue":
                summary["cash"] = float(item.value)
            elif item.tag == "EquityWithLoanValue":
                summary["equity"] = float(item.value)
            elif item.tag == "BuyingPower":
                summary["buying_power"] = float(item.value)
            elif item.tag == "NetLiquidation":
                summary["net_liquidation"] = float(item.value)

        return summary

    async def get_positions(self) -> Dict[str, int]:
        """Get current positions

        Returns:
            Dict mapping symbol -> quantity
        """
        if not self.connected:
            raise RuntimeError("Not connected to IBKR")

        positions = self.ib.positions()
        position_dict = {}

        for pos in positions:
            symbol = pos.contract.symbol
            quantity = int(pos.position)
            position_dict[symbol] = quantity

        return position_dict

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current market price for a symbol

        Args:
            symbol: Stock symbol

        Returns:
            Current price or None
        """
        if not self.connected:
            raise RuntimeError("Not connected to IBKR")

        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            # Request market data
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(0.5)  # Wait for data

            # Get price
            if ticker.last and ticker.last > 0:
                price = ticker.last
            elif ticker.close and ticker.close > 0:
                price = ticker.close
            elif ticker.bid and ticker.ask:
                price = (ticker.bid + ticker.ask) / 2
            else:
                price = None

            # Cancel market data
            self.ib.cancelMktData(contract)

            return price

        except Exception as e:
            print(f"Error getting price for {symbol}: {e}")
            return None

    async def place_market_order(
        self,
        symbol: str,
        action: OrderAction,
        quantity: int,
    ) -> Optional[Trade]:
        """Place a market order

        Args:
            symbol: Stock symbol
            action: BUY or SELL
            quantity: Number of shares

        Returns:
            Trade object or None if failed
        """
        import traceback

        if not self.connected:
            raise RuntimeError("Not connected to IBKR")

        if quantity <= 0:
            print(f"Invalid quantity: {quantity}")
            return None

        try:
            # Create contract
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            # Create order
            order = MarketOrder(action.value, quantity)

            # Place order (synchronous call, but IB uses its own event loop internally)
            trade = self.ib.placeOrder(contract, order)

            # Wait a bit for order to be submitted
            await asyncio.sleep(0.5)

            print(f"[IBKR] Placed {action.value} order: {symbol} x {quantity}")
            print(f"[IBKR] Order ID: {trade.order.orderId}, Status: {trade.orderStatus.status}")

            return trade

        except Exception as e:
            print(f"[IBKR ERROR] Failed to place order for {symbol}: {e}")
            print(f"[IBKR ERROR] Traceback: {traceback.format_exc()}")
            return None

    async def place_limit_order(
        self,
        symbol: str,
        action: OrderAction,
        quantity: int,
        limit_price: float,
    ) -> Optional[Trade]:
        """Place a limit order

        Args:
            symbol: Stock symbol
            action: BUY or SELL
            quantity: Number of shares
            limit_price: Limit price

        Returns:
            Trade object or None if failed
        """
        import traceback

        if not self.connected:
            raise RuntimeError("Not connected to IBKR")

        if quantity <= 0:
            print(f"Invalid quantity: {quantity}")
            return None

        try:
            # Create contract
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            # Create order
            order = LimitOrder(action.value, quantity, limit_price)

            # Place order (synchronous call, but IB uses its own event loop internally)
            trade = self.ib.placeOrder(contract, order)

            # Wait a bit for order to be submitted
            await asyncio.sleep(0.5)

            print(f"[IBKR] Placed {action.value} LIMIT order: {symbol} x {quantity} @ ${limit_price:.2f}")
            print(f"[IBKR] Order ID: {trade.order.orderId}, Status: {trade.orderStatus.status}")

            return trade

        except Exception as e:
            print(f"[IBKR ERROR] Failed to place limit order for {symbol}: {e}")
            print(f"[IBKR ERROR] Traceback: {traceback.format_exc()}")
            return None

    async def cancel_order(self, trade: Trade) -> bool:
        """Cancel an order

        Args:
            trade: Trade object to cancel

        Returns:
            True if successful
        """
        if not self.connected:
            raise RuntimeError("Not connected to IBKR")

        try:
            self.ib.cancelOrder(trade.order)
            await asyncio.sleep(0.5)
            print(f"Cancelled order: {trade.order.orderId}")
            return True
        except Exception as e:
            print(f"Error cancelling order: {e}")
            return False

    async def get_open_orders(self) -> list:
        """Get all open orders

        Returns:
            List of Trade objects
        """
        if not self.connected:
            raise RuntimeError("Not connected to IBKR")

        return self.ib.openTrades()

    async def wait_for_fill(self, trade: Trade, timeout: int = 60) -> bool:
        """Wait for an order to fill

        Args:
            trade: Trade object to monitor
            timeout: Timeout in seconds

        Returns:
            True if filled, False if timeout
        """
        start_time = datetime.now()

        while (datetime.now() - start_time).seconds < timeout:
            await asyncio.sleep(1)

            if trade.orderStatus.status == "Filled":
                print(f"Order filled: {trade.contract.symbol}")
                return True
            elif trade.orderStatus.status in ["Cancelled", "Inactive"]:
                print(f"Order cancelled/inactive: {trade.contract.symbol}")
                return False

        print(f"Order fill timeout: {trade.contract.symbol}")
        return False

    async def replace_limit_order(self, trade: Trade, new_limit_price: float) -> bool:
        """Replace an existing limit order price (simple modify & resubmit).

        Args:
            trade: Trade object to modify
            new_limit_price: New limit price

        Returns:
            True if modification submitted
        """
        if not self.connected:
            raise RuntimeError("Not connected to IBKR")
        try:
            order = trade.order
            if order.orderType != 'LMT':
                print(f"[IBKR] replace_limit_order: Unsupported orderType={order.orderType}")
                return False
            order.lmtPrice = float(new_limit_price)
            self.ib.placeOrder(trade.contract, order)
            await asyncio.sleep(0.5)
            print(f"[IBKR] Replaced order {order.orderId} new LMT={new_limit_price:.4f}")
            return True
        except Exception as e:
            print(f"[IBKR ERROR] replace_limit_order failed: {e}")
            return False

    async def place_bracket_limit(
        self,
        symbol: str,
        action: OrderAction,
        quantity: int,
        limit_price: float,
        take_profit_bps: Optional[int] = None,
        stop_loss_bps: Optional[int] = None,
    ) -> Optional[Tuple[Trade, Optional[Trade], Optional[Trade]]]:
        """Place a parent limit with OCO TP/SL children.

        Returns (parent_trade, tp_trade, sl_trade) or None on failure.
        """
        import math
        if not self.connected:
            raise RuntimeError("Not connected to IBKR")
        if quantity <= 0:
            print("Invalid quantity")
            return None
        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            parent = LimitOrder(action.value, quantity, float(limit_price))
            # Determine opposite action for exits
            exit_action = OrderAction.SELL if action == OrderAction.BUY else OrderAction.BUY
            tp_order = None
            sl_order = None
            if take_profit_bps is not None:
                if action == OrderAction.BUY:
                    tp_px = float(limit_price) * (1.0 + (take_profit_bps / 10000.0))
                else:
                    tp_px = float(limit_price) * (1.0 - (take_profit_bps / 10000.0))
                tp_order = LimitOrder(exit_action.value, quantity, round(tp_px, 4))
            if stop_loss_bps is not None:
                # IB STOP uses auxPrice
                sl_order = Order()
                sl_order.orderType = 'STP'
                sl_order.action = exit_action.value
                sl_order.totalQuantity = quantity
                if action == OrderAction.BUY:
                    sl_px = float(limit_price) * (1.0 - (stop_loss_bps / 10000.0))
                else:
                    sl_px = float(limit_price) * (1.0 + (stop_loss_bps / 10000.0))
                sl_order.auxPrice = round(sl_px, 4)

            # Transmit chain
            parent.transmit = False
            tp_trade_obj = None
            sl_trade_obj = None

            parent_trade = self.ib.placeOrder(contract, parent)
            await asyncio.sleep(0.2)
            parent_id = parent_trade.order.orderId

            if tp_order is not None:
                tp_order.parentId = parent_id
                tp_order.transmit = False
                tp_trade_obj = self.ib.placeOrder(contract, tp_order)
                await asyncio.sleep(0.2)

            if sl_order is not None:
                sl_order.parentId = parent_id
                sl_order.transmit = True  # last one transmits the chain
                sl_trade_obj = self.ib.placeOrder(contract, sl_order)
            else:
                # If only TP exists, make it transmit
                if tp_trade_obj is not None:
                    tp = tp_trade_obj.order
                    tp.transmit = True
                    self.ib.placeOrder(contract, tp)

            await asyncio.sleep(0.5)
            print(f"[IBKR] Bracket placed parent={parent_id}")
            return parent_trade, tp_trade_obj, sl_trade_obj
        except Exception as e:
            print(f"[IBKR ERROR] place_bracket_limit failed: {e}")
            return None
