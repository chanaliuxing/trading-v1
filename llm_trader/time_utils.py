# time_utils.py
from __future__ import annotations
from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta, timezone
import zoneinfo

US_EASTERN = zoneinfo.ZoneInfo("America/New_York")
HONGKONG = zoneinfo.ZoneInfo("Asia/Hong_Kong")

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _session_blocks(tz: zoneinfo.ZoneInfo, market: str) -> list[tuple[time, time]]:
    """Return local session blocks (start, end).
    US equities: single block 09:30-16:00 (no lunch).
    HK example: 09:30-12:00, 13:00-16:00 (lunch 12:00-13:00)."""
    if market.upper() in ("US", "NYSE", "NASDAQ"):
        return [(time(9, 30), time(16, 0))]
    if market.upper() in ("HK", "HKEX"):
        return [(time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))]
    # default assume US
    return [(time(9, 30), time(16, 0))]

def _in_block(local_dt: datetime, start: time, end: time) -> bool:
    t = local_dt.timetz()
    return (t >= start and t <= end)

def _minutes_between(t1: datetime, t2: datetime) -> int:
    return int(round((t2 - t1).total_seconds() / 60.0))

def build_time_context(
    *,
    tz_str: Optional[str] = None,
    market: str = "US",
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return unified time context for prompts.
    - tz_str optional; if None use US Eastern for US; HK for HK.
    - Handles lunch break for markets like HKEX.
    """
    now_utc = now_utc or _now_utc()
    tz = zoneinfo.ZoneInfo(tz_str) if tz_str else (US_EASTERN if market.upper() in ("US","NYSE","NASDAQ") else HONGKONG)
    local_now = now_utc.astimezone(tz)
    blocks = _session_blocks(tz, market)

    is_open = False
    is_lunch = False
    minutes_since_open = None
    minutes_until_close = None
    market_phase = "PREMARKET"

    # Iterate blocks to identify where we are
    total_open_mins = 0
    since_first_open = None
    until_last_close = None

    day_blocks = []
    for start_t, end_t in blocks:
        start_dt = local_now.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
        end_dt = local_now.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
        day_blocks.append((start_dt, end_dt))
        total_open_mins += _minutes_between(start_dt, end_dt)

    first_open, last_close = day_blocks[0][0], day_blocks[-1][1]
    since_first_open = _minutes_between(first_open, local_now)
    until_last_close = _minutes_between(local_now, last_close)

    for idx, (st, ed) in enumerate(day_blocks):
        if st <= local_now <= ed:
            is_open = True
            minutes_since_open = _minutes_between(st, local_now)
            minutes_until_close = _minutes_between(local_now, ed)
            market_phase = "RTH"
            break

    if (local_now > last_close):
        market_phase = "POSTMARKET"

    # detect lunch break (if any)
    if len(blocks) > 1 and not is_open:
        # local_now in lunch gap?
        for (st1, ed1), (st2, ed2) in zip(day_blocks, day_blocks[1:]):
            if ed1 < local_now < st2:
                is_lunch = True
                market_phase = "LUNCH_BREAK"

    # first 30/last 30 hints
    is_first_30min = is_open and (minutes_since_open is not None and minutes_since_open <= 30)
    is_last_30min  = (is_open and minutes_until_close is not None and minutes_until_close <= 30)

    return {
        "timestamp_utc": now_utc.isoformat(),
        "market_tz": str(tz),
        "local_time": local_now.isoformat(),
        "market": market.upper(),
        "is_open": is_open,
        "is_lunch_break": is_lunch,
        "market_phase": market_phase,          # PREMARKET / RTH / LUNCH_BREAK / POSTMARKET
        "minutes_since_first_open": max(0, since_first_open) if since_first_open is not None else None,
        "minutes_until_last_close": max(0, until_last_close) if until_last_close is not None else None,
        "minutes_since_block_open": minutes_since_open,
        "minutes_until_block_close": minutes_until_close,
        "is_first_30min": is_first_30min,
        "is_last_30min": is_last_30min,
        "session_blocks_local": [(a.isoformat(), b.isoformat()) for a,b in day_blocks],
    }
