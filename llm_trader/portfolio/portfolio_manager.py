"""Portfolio Manager - Manages positions, cash, and risk limits

V2.0: Integrated with MemorySystem for autonomous learning
"""

from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
import json

NY_TZ = ZoneInfo("America/New_York")

# V2.0: Import memory system
try:
    from llm_trader.utils.memory_system import MemorySystem, EpisodicMemory
except ImportError:
    # Fallback for backwards compatibility
    MemorySystem = None
    EpisodicMemory = None

# V2.1: Import commission utilities
from ..utils.commission import calculate_commission, estimate_round_trip_commission


@dataclass
class Position:
    """Represents a position in a stock"""
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float = 0.0
    last_update: datetime = field(default_factory=lambda: datetime.now(NY_TZ))
    entry_time: datetime = field(default_factory=lambda: datetime.now(NY_TZ))  # When position was first opened
    peak_pnl_pct: float = 0.0  # Track peak unrealized P&L % for trailing stop

    @property
    def market_value(self) -> float:
        """Current market value of position"""
        return self.quantity * self.current_price

    @property
    def cost_basis(self) -> float:
        """Total cost basis"""
        return self.quantity * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        """Unrealized P&L"""
        return self.market_value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> float:
        """Unrealized P&L percentage"""
        if self.cost_basis == 0:
            return 0.0
        return (self.unrealized_pnl / self.cost_basis) * 100

    @property
    def hold_duration_minutes(self) -> int:
        """How long position has been held (in minutes)"""
        return int((datetime.now(NY_TZ) - self.entry_time).total_seconds() / 60)

    @property
    def drawdown_from_peak(self) -> float:
        """Current drawdown from peak P&L % (for trailing stop)"""
        return self.peak_pnl_pct - self.unrealized_pnl_pct


class PortfolioManager:
    """Manages portfolio state, positions, and risk"""

    def __init__(
        self,
        initial_cash: float = 50000.0,
        max_position_pct: float = 0.90,  # Max 90% per position (LLM does risk control)
        max_total_exposure: float = 0.95,  # Max 95% total exposure
        max_positions: int = 10,  # Max 10 concurrent positions
        memory_system: Optional[Any] = None,  # V2.0: Optional MemorySystem
        allow_short_selling: bool = False,  # V2.2: Allow short selling (default: False)
    ):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.max_position_pct = max_position_pct
        self.max_total_exposure = max_total_exposure
        self.max_positions = max_positions
        self.allow_short_selling = allow_short_selling  # V2.2: Short selling flag

        self.positions: Dict[str, Position] = {}
        self.trade_history = []
        self.pnl_history = []

        # V2.1: Commission tracking
        self.total_commission_paid = 0.0  # Lifetime commission

        # V2.0: Memory system integration
        self.memory_system = memory_system
        self.pending_episodes: Dict[str, Any] = {}  # Track open positions for memory

    @property
    def total_market_value(self) -> float:
        """Total market value of all positions"""
        return sum(pos.market_value for pos in self.positions.values())

    @property
    def total_equity(self) -> float:
        """Total portfolio equity (cash + positions)"""
        return self.cash + self.total_market_value

    @property
    def total_pnl(self) -> float:
        """Total P&L (realized + unrealized, AFTER commission)"""
        return self.total_equity - self.initial_cash

    @property
    def net_pnl(self) -> float:
        """Net P&L after commission (same as total_pnl since commission already deducted from cash)"""
        return self.total_pnl

    @property
    def total_pnl_pct(self) -> float:
        """Total P&L percentage"""
        return (self.total_pnl / self.initial_cash) * 100

    @property
    def max_position_size(self) -> float:
        """Max dollar amount per position"""
        return self.total_equity * self.max_position_pct

    @property
    def available_buying_power(self) -> float:
        """Available cash for new positions"""
        max_allowed_exposure = self.total_equity * self.max_total_exposure
        current_exposure = self.total_market_value
        return min(self.cash, max_allowed_exposure - current_exposure)

    def get_consecutive_wins_losses(self) -> tuple[int, int]:
        """Get count of consecutive wins and losses from recent trades

        Returns:
            (consecutive_wins, consecutive_losses)
        """
        if not self.trade_history:
            return 0, 0

        # Get recent closed trades (SELL actions with realized_pnl)
        recent_trades = [t for t in reversed(self.trade_history) if t.get('action') == 'SELL']

        consecutive_wins = 0
        consecutive_losses = 0

        for trade in recent_trades:
            realized_pnl = trade.get('realized_pnl', 0)
            if realized_pnl > 0:
                if consecutive_losses > 0:
                    break  # Stop at first non-loss
                consecutive_wins += 1
            elif realized_pnl < 0:
                if consecutive_wins > 0:
                    break  # Stop at first non-win
                consecutive_losses += 1
            else:
                break  # Break even, end streak

        return consecutive_wins, consecutive_losses

    def get_daily_pnl(self) -> float:
        """Get today's total realized P&L

        Returns:
            Total realized P&L for today
        """
        today = datetime.now(NY_TZ).date()
        daily_pnl = 0.0

        for trade in self.trade_history:
            trade_date = datetime.fromisoformat(trade['timestamp']).date()
            if trade_date == today and trade.get('action') == 'SELL':
                daily_pnl += trade.get('realized_pnl', 0)

        return daily_pnl

    def get_daily_pnl_pct(self) -> float:
        """Get today's total realized P&L as percentage of initial capital

        Returns:
            Daily P&L percentage
        """
        return (self.get_daily_pnl() / self.initial_cash) * 100

    def update_position_prices(self, prices: Dict[str, float]):
        """Update current prices for all positions

        Args:
            prices: Dict mapping symbol -> current price
        """
        for symbol, price in prices.items():
            if symbol in self.positions:
                pos = self.positions[symbol]
                pos.current_price = price
                pos.last_update = datetime.now(NY_TZ)

                # Update peak P&L % for trailing stop tracking
                current_pnl_pct = pos.unrealized_pnl_pct
                if current_pnl_pct > pos.peak_pnl_pct:
                    pos.peak_pnl_pct = current_pnl_pct

    def can_open_position(self, symbol: str, price: float, quantity: int) -> tuple[bool, str]:
        """Check if can open a new position

        Args:
            symbol: Stock symbol
            price: Current price
            quantity: Desired quantity

        Returns:
            (can_open, reason)
        """
        # Check if already at max positions
        if symbol not in self.positions and len(self.positions) >= self.max_positions:
            return False, f"Already at max positions ({self.max_positions})"

        # Calculate required capital
        required_capital = price * quantity

        # Check if have enough cash
        if required_capital > self.cash:
            return False, f"Insufficient cash (need ${required_capital:.2f}, have ${self.cash:.2f})"

        # Check if within buying power
        if required_capital > self.available_buying_power:
            return False, f"Exceeds buying power (${self.available_buying_power:.2f})"

        # Check if position size exceeds max
        if required_capital > self.max_position_size:
            return False, f"Exceeds max position size (${self.max_position_size:.2f})"

        return True, "OK"

    def can_close_position(self, symbol: str, quantity: int) -> tuple[bool, str]:
        """Check if can close/reduce a position (long or short)

        Args:
            symbol: Stock symbol
            quantity: Quantity to close (always positive)

        Returns:
            (can_close, reason)
        """
        if symbol not in self.positions:
            return False, f"No position in {symbol}"

        # V2.2: Support both long and short positions
        pos_quantity = abs(self.positions[symbol].quantity)
        if quantity > pos_quantity:
            return False, f"Insufficient quantity (have {pos_quantity}, want to close {quantity})"

        return True, "OK"

    def open_position(self, symbol: str, quantity: int, price: float, is_short: bool = False) -> bool:
        """Open or add to a position (long or short)

        Args:
            symbol: Stock symbol
            quantity: Number of shares (positive for long, will be negated if short)
            price: Entry price
            is_short: If True, open short position (default: False)

        Returns:
            True if successful
        """
        # V2.2: Handle short selling
        if is_short:
            if not self.allow_short_selling:
                print(f"Cannot open short position: Short selling is disabled")
                return False
            # Make quantity negative for short positions
            quantity = -abs(quantity)
            action = "SHORT"
        else:
            # Ensure quantity is positive for long positions
            quantity = abs(quantity)
            action = "BUY"

        can_open, reason = self.can_open_position(symbol, price, abs(quantity))
        if not can_open:
            print(f"Cannot open position: {reason}")
            return False

        cost = abs(quantity) * price
        # V2.1: Calculate commission (0.01%, min $1)
        commission = calculate_commission(cost)

        if symbol in self.positions:
            # Add to existing position (average cost)
            pos = self.positions[symbol]

            # V2.2: Check if adding to same direction or reversing
            if (pos.quantity > 0 and quantity > 0) or (pos.quantity < 0 and quantity < 0):
                # Same direction - average the cost
                total_quantity = pos.quantity + quantity
                total_cost = abs(pos.cost_basis) + cost
                pos.quantity = total_quantity
                pos.avg_cost = total_cost / abs(total_quantity)
                pos.current_price = price
                pos.last_update = datetime.now(NY_TZ)
            else:
                # Opposite direction - this is a close/reduce, not handled here
                print(f"Cannot add opposite direction position. Use close_position instead.")
                return False
        else:
            # Create new position
            self.positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                avg_cost=price,
                current_price=price,
            )

        # V2.2: For short positions, we receive cash; for long, we pay cash
        if is_short:
            # Short selling: receive cash (minus commission)
            self.cash += (cost - commission)
        else:
            # Long position: pay cash (plus commission)
            self.cash -= (cost + commission)

        self.total_commission_paid += commission

        # Record trade
        trade_record = {
            "timestamp": datetime.now(NY_TZ).isoformat(),
            "action": action,
            "symbol": symbol,
            "quantity": abs(quantity),  # Always record positive quantity
            "price": price,
            "cost": cost,
            "commission": commission,
            "cash_after": self.cash,
        }
        self.trade_history.append(trade_record)

        # V2.0: Record to memory system (if enabled)
        if self.memory_system:
            # Store entry timestamp for calculating hold duration later
            self.pending_episodes[symbol] = {
                "entry_time": datetime.now(NY_TZ),
                "entry_price": price,
                "quantity": quantity,
            }

        position_type = "SHORT" if is_short else "LONG"
        print(f"Opened {position_type} position: {symbol} x {abs(quantity)} @ ${price:.2f} (cost: ${cost:.2f}, commission: ${commission:.2f})")
        return True

    def close_position(self, symbol: str, quantity: int, price: float) -> bool:
        """Close or reduce a position (long or short)

        Args:
            symbol: Stock symbol
            quantity: Number of shares to close (always positive)
            price: Close price

        Returns:
            True if successful
        """
        can_close, reason = self.can_close_position(symbol, quantity)
        if not can_close:
            print(f"Cannot close position: {reason}")
            return False

        pos = self.positions[symbol]
        is_short = pos.quantity < 0  # V2.2: Detect if closing short position

        # V2.2: Handle both long and short closing
        if is_short:
            # Closing short: buy back shares
            # P&L = entry_proceeds - buyback_cost
            entry_proceeds = quantity * pos.avg_cost
            buyback_cost = quantity * price
            commission = calculate_commission(buyback_cost)

            gross_pnl = entry_proceeds - buyback_cost
            realized_pnl = gross_pnl - commission

            # Pay to buy back shares (plus commission)
            self.cash -= (buyback_cost + commission)
            action = "COVER"
        else:
            # Closing long: sell shares
            proceeds = quantity * price
            cost_basis = quantity * pos.avg_cost
            commission = calculate_commission(proceeds)

            gross_pnl = proceeds - cost_basis
            realized_pnl = gross_pnl - commission

            # Receive proceeds (minus commission)
            self.cash += (proceeds - commission)
            action = "SELL"

        self.total_commission_paid += commission

        # Update position
        if quantity == abs(pos.quantity):
            # Close entire position
            del self.positions[symbol]
        else:
            # Reduce position
            if is_short:
                pos.quantity += quantity  # Short: -10 + 5 = -5 (reduced short)
            else:
                pos.quantity -= quantity  # Long: 10 - 5 = 5 (reduced long)
            pos.current_price = price
            pos.last_update = datetime.now(NY_TZ)

        # Record trade
        trade_record = {
            "timestamp": datetime.now(NY_TZ).isoformat(),
            "action": action,  # V2.2: SELL or COVER
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "commission": commission,
            "gross_pnl": gross_pnl,
            "realized_pnl": realized_pnl,
            "cash_after": self.cash,
        }
        self.trade_history.append(trade_record)

        # V2.0: Update memory system with outcome (if enabled)
        if self.memory_system and symbol in self.pending_episodes:
            episode_data = self.pending_episodes[symbol]
            entry_time = episode_data["entry_time"]
            hold_duration = int((datetime.now(NY_TZ) - entry_time).total_seconds() / 60)

            # Update the episode outcome
            # Note: The episode should have been created when decision was made
            # Here we just update the outcome if it exists in recent episodes
            recent_episodes = self.memory_system.get_recent_episodes(n=50)
            for episode in reversed(recent_episodes):
                if (episode.symbol == symbol and
                    episode.action == "BUY" and
                    not episode.outcome_verified):
                    self.memory_system.update_episode_outcome(
                        episode=episode,
                        realized_pnl=realized_pnl,
                        hold_duration_minutes=hold_duration,
                    )
                    break

            # Clean up pending
            del self.pending_episodes[symbol]

        print(f"Closed position: {symbol} x {quantity} @ ${price:.2f} (P&L: ${realized_pnl:.2f} after ${commission:.2f} commission)")
        return True

    def get_position_state(self) -> Dict[str, Any]:
        """Get current portfolio state for LLM with risk management metrics

        Returns:
            Dict with portfolio state including risk metrics
        """
        positions_dict = {
            symbol: pos.quantity for symbol, pos in self.positions.items()
        }

        # Get detailed position info for risk management
        positions_detail = {}
        for symbol, pos in self.positions.items():
            positions_detail[symbol] = {
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "current_price": pos.current_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
                "peak_pnl_pct": pos.peak_pnl_pct,
                "drawdown_from_peak": pos.drawdown_from_peak,
                "hold_duration_minutes": pos.hold_duration_minutes,
            }

        # Get consecutive wins/losses
        consecutive_wins, consecutive_losses = self.get_consecutive_wins_losses()

        # Get daily P&L
        daily_pnl = self.get_daily_pnl()
        daily_pnl_pct = self.get_daily_pnl_pct()

        return {
            "cash": self.cash,
            "total_equity": self.total_equity,
            "total_value": self.total_equity,
            "positions": positions_dict,  # Simple dict for backward compatibility
            "positions_detail": positions_detail,  # Detailed info with risk metrics
            "num_positions": len(self.positions),
            "max_position_size": self.max_position_size,
            "available_buying_power": self.available_buying_power,
            "total_pnl": self.total_pnl,
            "total_pnl_pct": self.total_pnl_pct,
            # V2.1: Commission tracking
            "total_commission_paid": self.total_commission_paid,
            # Risk management metrics
            "consecutive_wins": consecutive_wins,
            "consecutive_losses": consecutive_losses,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
        }

    def get_detailed_state(self) -> Dict[str, Any]:
        """Get detailed portfolio state

        Returns:
            Dict with detailed state
        """
        positions_detail = {}
        for symbol, pos in self.positions.items():
            positions_detail[symbol] = {
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                "unrealized_pnl": pos.unrealized_pnl,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
            }

        return {
            "timestamp": datetime.now(NY_TZ).isoformat(),
            "cash": self.cash,
            "total_market_value": self.total_market_value,
            "total_equity": self.total_equity,
            "total_pnl": self.total_pnl,
            "total_pnl_pct": self.total_pnl_pct,
            "positions": positions_detail,
            "num_positions": len(self.positions),
            "max_position_size": self.max_position_size,
            "available_buying_power": self.available_buying_power,
        }

    def save_state(self, filepath: str):
        """Save portfolio state to file

        Args:
            filepath: Path to save file
        """
        state = {
            "portfolio": self.get_detailed_state(),
            "trade_history": self.trade_history,
        }

        with open(filepath, "w") as f:
            json.dump(state, f, indent=2)

        print(f"Portfolio state saved to {filepath}")

        # V2.0: Save memory system separately (if enabled)
        if self.memory_system:
            memory_filepath = filepath.replace(".json", "_memory.json")
            self.memory_system.save_to_file(memory_filepath)
            print(f"Memory system saved to {memory_filepath}")
