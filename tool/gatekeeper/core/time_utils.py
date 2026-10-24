from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def minutes_to_us_equity_close(now: datetime | None = None) -> int:
    ny = ZoneInfo("America/New_York")
    if now is None:
        now = datetime.now(tz=ny)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ny)
    else:
        now = now.astimezone(ny)

    if now.weekday() >= 5:
        return 9999

    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= close_dt:
        return 0

    delta = close_dt - now
    return max(0, int(delta.total_seconds() // 60))


def us_equity_session_lookback_min(now: datetime | None = None) -> int:
    ny = ZoneInfo("America/New_York")
    if now is None:
        now = datetime.now(tz=ny)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ny)
    else:
        now = now.astimezone(ny)

    session_minutes = 390
    if now.weekday() >= 5:
        return session_minutes

    open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < open_dt or now >= close_dt:
        return session_minutes

    elapsed = int((now - open_dt).total_seconds() // 60) + 1
    return max(1, min(session_minutes, elapsed))


def iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
