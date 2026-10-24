"""
Market Calendar
Based on doc/39.md specification

Provides unified RTH (Regular Trading Hours) detection.

Features:
- NYSE calendar (09:30-16:00 ET)
- Holiday support (configurable)
- Half-day support (early close)
- Pre/post market session support
- Next open/close calculation
"""
import datetime as dt
from typing import Optional, Set, Dict, Tuple

import pytz

from libs.logger import get_logger

logger = get_logger(__name__)


class MarketCalendar:
    """
    US market calendar (NYSE)

    Provides RTH detection and trading session information.
    """

    # US market holidays 2025
    US_HOLIDAYS_2025 = {
        "2025-01-01",  # New Year's Day
        "2025-01-20",  # MLK Day
        "2025-02-17",  # Presidents' Day
        "2025-04-18",  # Good Friday
        "2025-05-26",  # Memorial Day
        "2025-06-19",  # Juneteenth
        "2025-07-04",  # Independence Day
        "2025-09-01",  # Labor Day
        "2025-11-27",  # Thanksgiving
        "2025-12-25",  # Christmas
    }

    # Half days (early close at 13:00 ET)
    HALF_DAYS_2025 = {
        "2025-11-28": "13:00",  # Day after Thanksgiving
        "2025-12-24": "13:00",  # Christmas Eve
    }

    def __init__(
        self,
        tz: str = "America/New_York",
        rth_open_hm: str = "09:30",
        rth_close_hm: str = "16:00",
        holiday_set: Optional[Set[str]] = None,
        half_days: Optional[Dict[str, str]] = None
    ):
        """
        Initialize market calendar

        Args:
            tz: Timezone (default: America/New_York)
            rth_open_hm: RTH open time (HH:MM)
            rth_close_hm: RTH close time (HH:MM)
            holiday_set: Set of holiday dates (YYYY-MM-DD)
            half_days: Dict of half days {date: close_time}
        """
        self.tz = pytz.timezone(tz)
        self.rth_open_hm = rth_open_hm
        self.rth_close_hm = rth_close_hm
        self._holidays = holiday_set or self.US_HOLIDAYS_2025
        self._half_days = half_days or self.HALF_DAYS_2025

        logger.info(
            "MarketCalendar initialized",
            tz=tz,
            rth_open=rth_open_hm,
            rth_close=rth_close_hm,
            holidays=len(self._holidays),
            half_days=len(self._half_days)
        )

    def _local_date(self, t: Optional[dt.datetime] = None) -> dt.date:
        """
        Get local date

        Args:
            t: Datetime (default: now)

        Returns:
            Date in market timezone
        """
        now = t or dt.datetime.now(tz=self.tz)
        return now.date()

    def _hm_to_time(self, hm: str) -> dt.time:
        """
        Convert HH:MM to time object

        Args:
            hm: Time string (HH:MM)

        Returns:
            Time object (naive, will be localized later)
        """
        hh, mm = hm.split(":")
        return dt.time(int(hh), int(mm))

    def is_trading_day(self, d: Optional[dt.date] = None) -> bool:
        """
        Check if date is a trading day

        Args:
            d: Date (default: today)

        Returns:
            True if trading day
        """
        d = d or self._local_date()

        # Weekend
        if d.weekday() >= 5:  # Saturday/Sunday
            return False

        # Holiday
        if d.strftime("%Y-%m-%d") in self._holidays:
            return False

        return True

    def rth_window(
        self,
        d: Optional[dt.date] = None
    ) -> Tuple[dt.datetime, dt.datetime]:
        """
        Get RTH open and close times for date

        Args:
            d: Date (default: today)

        Returns:
            (open_datetime, close_datetime)
        """
        d = d or self._local_date()

        open_hm = self.rth_open_hm
        date_str = d.strftime("%Y-%m-%d")

        # Check for half day (early close)
        close_hm = self._half_days.get(date_str, self.rth_close_hm)

        # Build datetimes
        open_dt = self.tz.localize(
            dt.datetime.combine(d, self._hm_to_time(open_hm))
        )
        close_dt = self.tz.localize(
            dt.datetime.combine(d, self._hm_to_time(close_hm))
        )

        return open_dt, close_dt

    def is_rth(
        self,
        now: Optional[dt.datetime] = None,
        allow_prepost: bool = False
    ) -> bool:
        """
        Check if current time is RTH

        Args:
            now: Datetime (default: now)
            allow_prepost: If True, allow pre/post market

        Returns:
            True if RTH (or pre/post if allowed)
        """
        now = now.astimezone(self.tz) if now else dt.datetime.now(self.tz)

        # Check if trading day
        if not self.is_trading_day(now.date()):
            return False

        # Get RTH window
        open_dt, close_dt = self.rth_window(now.date())

        # Check if in RTH
        if open_dt <= now <= close_dt:
            return True

        # Allow pre/post market if configured
        return allow_prepost

    def next_open_close(
        self,
        now: Optional[dt.datetime] = None
    ) -> Tuple[dt.datetime, dt.datetime]:
        """
        Get next RTH open and close times

        Args:
            now: Datetime (default: now)

        Returns:
            (next_open, next_close)
        """
        now = now.astimezone(self.tz) if now else dt.datetime.now(self.tz)
        d = now.date()

        # Look ahead up to 7 days
        for i in range(8):
            dd = d + dt.timedelta(days=i)

            if not self.is_trading_day(dd):
                continue

            open_dt, close_dt = self.rth_window(dd)

            # If we haven't passed close yet, return this session
            if now <= close_dt:
                return open_dt, close_dt

        # Fallback (shouldn't happen)
        return self.rth_window(d)

    def session_status(
        self,
        now: Optional[dt.datetime] = None
    ) -> str:
        """
        Get current session status

        Args:
            now: Datetime (default: now)

        Returns:
            Status: "pre_market", "rth", "post_market", "closed"
        """
        now = now.astimezone(self.tz) if now else dt.datetime.now(self.tz)

        if not self.is_trading_day(now.date()):
            return "closed"

        open_dt, close_dt = self.rth_window(now.date())

        if now < open_dt:
            return "pre_market"
        elif open_dt <= now <= close_dt:
            return "rth"
        elif now > close_dt:
            return "post_market"

        return "closed"

    def is_half_day(self, d: Optional[dt.date] = None) -> bool:
        """
        Check if date is a half day

        Args:
            d: Date (default: today)

        Returns:
            True if half day
        """
        d = d or self._local_date()
        return d.strftime("%Y-%m-%d") in self._half_days

    def minutes_to_open(self, now: Optional[dt.datetime] = None) -> float:
        """
        Minutes until next market open

        Args:
            now: Datetime (default: now)

        Returns:
            Minutes to open (negative if already open)
        """
        now = now.astimezone(self.tz) if now else dt.datetime.now(self.tz)
        next_open, _ = self.next_open_close(now)

        delta = (next_open - now).total_seconds() / 60.0
        return delta

    def minutes_to_close(self, now: Optional[dt.datetime] = None) -> float:
        """
        Minutes until next market close

        Args:
            now: Datetime (default: now)

        Returns:
            Minutes to close (negative if already closed)
        """
        now = now.astimezone(self.tz) if now else dt.datetime.now(self.tz)
        _, next_close = self.next_open_close(now)

        delta = (next_close - now).total_seconds() / 60.0
        return delta
