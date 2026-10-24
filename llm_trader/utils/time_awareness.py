"""Time Awareness Module - Provides market-aware time context for LLM decisions"""

from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from typing import Dict, Any, Literal
from enum import Enum


class MarketPhase(str, Enum):
    """Market trading phases"""
    PREMARKET = "PREMARKET"      # 4:00 - 9:30 ET
    OPEN = "OPEN"                # 9:30 - 10:30 ET (first hour)
    MIDDAY = "MIDDAY"            # 10:30 - 15:00 ET
    CLOSE = "CLOSE"              # 15:00 - 16:00 ET (last hour)
    AFTERHOURS = "AFTERHOURS"    # 16:00 - 20:00 ET
    CLOSED = "CLOSED"            # 20:00 - 4:00 ET


class TimeAwareness:
    """Provides time-aware context for trading decisions"""

    def __init__(self, timezone: str = "America/New_York"):
        """Initialize time awareness

        Args:
            timezone: Market timezone (default: America/New_York for US markets)
        """
        self.timezone = ZoneInfo(timezone)

        # US market hours (Eastern Time)
        self.premarket_open = dt_time(4, 0)
        self.market_open = dt_time(9, 30)
        self.market_close = dt_time(16, 0)
        self.afterhours_close = dt_time(20, 0)

    def now_market_time(self) -> datetime:
        """Get current time in market timezone"""
        return datetime.now(self.timezone)

    def now_utc(self) -> datetime:
        """Get current UTC time"""
        return datetime.now(ZoneInfo("UTC"))

    def get_market_phase(self, dt: datetime = None) -> MarketPhase:
        """Get current market phase

        Args:
            dt: Datetime to check (default: now)

        Returns:
            MarketPhase enum
        """
        if dt is None:
            dt = self.now_market_time()
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.timezone)
        else:
            dt = dt.astimezone(self.timezone)

        current_time = dt.time()

        if current_time < self.premarket_open or current_time >= self.afterhours_close:
            return MarketPhase.CLOSED
        elif current_time < self.market_open:
            return MarketPhase.PREMARKET
        elif current_time < dt_time(10, 30):
            return MarketPhase.OPEN
        elif current_time < dt_time(15, 0):
            return MarketPhase.MIDDAY
        elif current_time < self.market_close:
            return MarketPhase.CLOSE
        else:
            return MarketPhase.AFTERHOURS

    def is_market_open(self, dt: datetime = None) -> bool:
        """Check if market is currently open for regular trading

        Args:
            dt: Datetime to check (default: now)

        Returns:
            True if market is open
        """
        phase = self.get_market_phase(dt)
        return phase in [MarketPhase.OPEN, MarketPhase.MIDDAY, MarketPhase.CLOSE]

    def is_extended_hours(self, dt: datetime = None) -> bool:
        """Check if in extended hours (premarket or afterhours)

        Args:
            dt: Datetime to check (default: now)

        Returns:
            True if in extended hours
        """
        phase = self.get_market_phase(dt)
        return phase in [MarketPhase.PREMARKET, MarketPhase.AFTERHOURS]

    def minutes_to_open(self, dt: datetime = None) -> int:
        """Calculate minutes until market open

        Args:
            dt: Datetime to check (default: now)

        Returns:
            Minutes to open (negative if market is already open)
        """
        if dt is None:
            dt = self.now_market_time()
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.timezone)
        else:
            dt = dt.astimezone(self.timezone)

        # Create market open datetime for today
        market_open_dt = dt.replace(
            hour=self.market_open.hour,
            minute=self.market_open.minute,
            second=0,
            microsecond=0
        )

        # If already past open, calculate for tomorrow
        if dt.time() >= self.market_open:
            from datetime import timedelta
            market_open_dt += timedelta(days=1)

        delta = market_open_dt - dt
        return int(delta.total_seconds() / 60)

    def minutes_to_close(self, dt: datetime = None) -> int:
        """Calculate minutes until market close

        Args:
            dt: Datetime to check (default: now)

        Returns:
            Minutes to close (negative if market is already closed)
        """
        if dt is None:
            dt = self.now_market_time()
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.timezone)
        else:
            dt = dt.astimezone(self.timezone)

        # Create market close datetime for today
        market_close_dt = dt.replace(
            hour=self.market_close.hour,
            minute=self.market_close.minute,
            second=0,
            microsecond=0
        )

        # If already past close, return negative
        if dt.time() >= self.market_close:
            return -1

        delta = market_close_dt - dt
        return int(delta.total_seconds() / 60)

    def get_time_context(self, dt: datetime = None) -> Dict[str, Any]:
        """Get comprehensive time context for LLM decision making

        Args:
            dt: Datetime to check (default: now)

        Returns:
            Dict with time context information
        """
        if dt is None:
            dt = self.now_market_time()

        utc_now = self.now_utc()
        phase = self.get_market_phase(dt)
        is_open = self.is_market_open(dt)

        context = {
            "utc_time": utc_now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "local_time": dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "market_phase": phase.value,
            "is_market_open": is_open,
            "is_extended_hours": self.is_extended_hours(dt),
            "minutes_to_open": self.minutes_to_open(dt) if not is_open else None,
            "minutes_to_close": self.minutes_to_close(dt) if is_open else None,
            "day_of_week": dt.strftime("%A"),
            "timestamp_unix": int(dt.timestamp()),
        }

        # Add phase-specific context
        if phase == MarketPhase.OPEN:
            context["phase_description"] = "Market just opened - high volatility expected"
            context["suggested_frequency"] = "high"  # 15-30s
        elif phase == MarketPhase.CLOSE:
            context["phase_description"] = "Market closing soon - consider closing positions"
            context["suggested_frequency"] = "high"  # 15-30s
        elif phase == MarketPhase.MIDDAY:
            context["phase_description"] = "Midday trading - normal volatility"
            context["suggested_frequency"] = "medium"  # 60s
        elif phase == MarketPhase.PREMARKET:
            mins_to_open = self.minutes_to_open(dt)
            if mins_to_open <= 30:
                context["phase_description"] = f"Premarket - {mins_to_open}min to open, prepare positions"
                context["suggested_frequency"] = "high"
            else:
                context["phase_description"] = "Premarket - too early for trading"
                context["suggested_frequency"] = "low"
        else:
            context["phase_description"] = "Market closed"
            context["suggested_frequency"] = "none"

        return context

    def should_trade(self, dt: datetime = None, allow_extended: bool = False) -> bool:
        """Determine if trading should be allowed at this time

        Args:
            dt: Datetime to check (default: now)
            allow_extended: Allow trading in extended hours

        Returns:
            True if trading should be allowed
        """
        phase = self.get_market_phase(dt)

        if phase == MarketPhase.CLOSED:
            return False

        if not allow_extended and phase in [MarketPhase.PREMARKET, MarketPhase.AFTERHOURS]:
            return False

        return True

    def get_adaptive_interval(self, has_positions: bool = False, dt: datetime = None) -> int:
        """Get adaptive polling interval based on market phase and position status

        Args:
            has_positions: Whether currently holding positions
            dt: Datetime to check (default: now)

        Returns:
            Recommended interval in seconds
        """
        phase = self.get_market_phase(dt)

        if phase == MarketPhase.CLOSED:
            return 300  # 5 minutes (mostly idle)

        if phase == MarketPhase.PREMARKET:
            mins_to_open = self.minutes_to_open(dt)
            if mins_to_open <= 30:
                return 30 if has_positions else 60  # Active monitoring before open
            else:
                return 120  # Low frequency early premarket

        if phase == MarketPhase.AFTERHOURS:
            return 60 if has_positions else 120

        # Regular trading hours
        if phase == MarketPhase.OPEN or phase == MarketPhase.CLOSE:
            # High frequency during open/close
            return 15 if has_positions else 30
        else:
            # Midday - medium frequency
            return 30 if has_positions else 60

    def format_for_prompt(self, dt: datetime = None) -> str:
        """Format time context for LLM prompt

        Args:
            dt: Datetime to check (default: now)

        Returns:
            Formatted string for prompt
        """
        context = self.get_time_context(dt)

        prompt = f"""TIME CONTEXT:
Current Time: {context['local_time']} ({context['utc_time']})
Market Phase: {context['market_phase']} - {context['phase_description']}
Day: {context['day_of_week']}
"""

        if context.get('minutes_to_open') is not None and context['minutes_to_open'] > 0:
            prompt += f"Minutes to Market Open: {context['minutes_to_open']}\n"

        if context.get('minutes_to_close') is not None and context['minutes_to_close'] > 0:
            prompt += f"Minutes to Market Close: {context['minutes_to_close']}\n"

        return prompt
