from __future__ import annotations

import os
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from functools import lru_cache

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOLIDAY_FILE = PROJECT_ROOT / "config" / "us_market_holidays_2024_2030.json"
LAST_DATA_TABLE = "trading.bars_1m_ibkr"

def last_data_day(symbols=None):
    """
    Return the last trading day that has bars in bars_1m_ibkr for given symbols.
    Returns None if no bars are found.
    """
    try:
        from libs.db.ch_pool import get_client
        ch = get_client()
        syms = symbols or ["TSLA", "NVDA"]
        res = ch.query(
            f"""
            SELECT toDate(maxIf(ts_end, ts_end <= now() AND is_rth = 1)) AS max_day
            FROM {LAST_DATA_TABLE}
            WHERE symbol IN %(syms)s
            """,
            {"syms": syms},
        )
        max_day = None
        if res and res[0].get("max_day"):
            max_day = res[0]["max_day"]
        else:
            # Fallback: ignore is_rth filter if no result
            res2 = ch.query(
                f"""
                SELECT toDate(maxIf(ts_end, ts_end <= now())) AS max_day
                FROM {LAST_DATA_TABLE}
                WHERE symbol IN %(syms)s
                """,
                {"syms": syms},
            )
            if res2 and res2[0].get("max_day"):
                max_day = res2[0]["max_day"]
        if max_day:
            try:
                return max_day
            except Exception:
                pass
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def _load_holidays() -> dict[str, list[str]]:
    try:
        if HOLIDAY_FILE.exists():
            return json.loads(HOLIDAY_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def is_market_closed(check_date: date | None = None) -> bool:
    """
    Returns True if the market is considered closed for the given date (or today).
    Logic: HOLIDAY_MODE override → weekend → holiday file.
    """
    if os.getenv("HOLIDAY_MODE", "").lower() in ("1", "true", "yes", "on"):
        return True
    d = check_date or datetime.now().date()
    if d.weekday() >= 5:
        return True
    holidays = _load_holidays()
    return d.isoformat() in holidays.get(str(d.year), [])


def is_early_close_day(check_date: date | None = None) -> bool:
    """
    Returns True for common US equity early-close sessions (13:00 ET).
    """
    d = check_date or datetime.now().date()
    if d.weekday() >= 5:
        return False
    if is_market_closed(d):
        return False

    # Christmas Eve
    if d.month == 12 and d.day == 24:
        return True

    # Day after Thanksgiving (Friday after the 4th Thursday of November)
    if d.month == 11 and d.weekday() == 4:  # Friday
        first = date(d.year, 11, 1)
        first_thu_offset = (3 - first.weekday()) % 7
        fourth_thu = first + timedelta(days=first_thu_offset + 21)
        if d == fourth_thu + timedelta(days=1):
            return True

    # Independence Day eve (if it's a weekday and the market is open)
    if d.month == 7 and d.day == 3:
        return True

    return False


def last_trading_day(ref_date: date | None = None) -> date:
    """Return the most recent trading day on or before ref_date (weekend/holiday skipped)."""
    d = ref_date or datetime.now().date()
    while True:
        if not is_market_closed(d):
            return d
        d = d - timedelta(days=1)


def previous_trading_day(ref_date: date | None = None) -> date:
    """Return the most recent trading day strictly before ref_date."""
    d = (ref_date or datetime.now().date()) - timedelta(days=1)
    return last_trading_day(d)
