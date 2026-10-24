"""
NY Time Utilities - Unified timezone handling for the entire trading system

All times in this system use America/New_York timezone to avoid confusion.
This module provides the single source of truth for time-related operations.
"""
from __future__ import annotations
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def now_ny() -> datetime:
    """Return timezone-aware 'America/New_York' datetime (canonical in this system)."""
    return datetime.now(NY_TZ)


def to_ny(dt: datetime) -> datetime:
    """Convert any datetime to NY timezone."""
    if dt.tzinfo is None:
        # treat naive as NY by convention to avoid mixed TZ usage
        return dt.replace(tzinfo=NY_TZ)
    return dt.astimezone(NY_TZ)


def ny_trading_day(dt: datetime | None = None) -> date:
    """Return NY trading day (calendar date in NY timezone)."""
    return (dt or now_ny()).date()


def fmt_ny(dt: datetime | None = None, ms: bool = True) -> str:
    """Format NY time as string; keep consistent everywhere when writing to DB/logs."""
    dt = dt or now_ny()
    if ms:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def seconds_until(dt_target: datetime, dt_now: datetime | None = None) -> int:
    """Seconds from now (NY) to target (NY)."""
    dt_now = to_ny(dt_now or now_ny())
    dt_target = to_ny(dt_target)
    return max(0, int((dt_target - dt_now).total_seconds()))


def next_market_close_ny(dt: datetime | None = None) -> datetime:
    """Return today's 16:00 NY time (no holiday logic; you already run only on trading days)."""
    dt = to_ny(dt or now_ny())
    return datetime(dt.year, dt.month, dt.day, 16, 0, 0, tzinfo=NY_TZ)
