"""
Market calendar utilities (XNYS) with graceful fallbacks.

Priority:
- If pandas_market_calendars is available → use XNYS schedule
- Else use config.yml > market_calendar.holidays (YYYY-MM-DD) as holiday list

Functions:
- is_holiday(date) -> bool
- is_trading_day(date) -> bool
"""
from __future__ import annotations
from datetime import date as _date
from typing import List

try:
    import pandas as _pd  # type: ignore
except Exception:  # pragma: no cover
    _pd = None  # type: ignore

try:
    import pandas_market_calendars as _pmc  # type: ignore
except Exception:  # pragma: no cover
    _pmc = None  # type: ignore

from .config import config as _cfg


def _cfg_holidays() -> List[str]:
    try:
        mc = _cfg.get('market_calendar', {}) or {}
        hol = mc.get('holidays') or []
        return [str(d) for d in hol]
    except Exception:
        return []


def is_holiday(d: _date) -> bool:
    """Return True if date is a market holiday (XNYS)."""
    if d.weekday() >= 5:
        return True
    # Try pandas_market_calendars if available
    if _pmc is not None and _pd is not None:
        try:
            cal = _pmc.get_calendar('XNYS')
            sched = cal.schedule(start_date=d, end_date=d)
            # If schedule empty → holiday or weekend
            if sched is None or len(sched) == 0:
                # Could be weekend; treat as holiday-equivalent for trading
                return True
            return False
        except Exception:
            pass
    # Fallback to config holidays
    try:
        return d.isoformat() in set(_cfg_holidays())
    except Exception:
        return False


def is_trading_day(d: _date) -> bool:
    return not is_holiday(d)
