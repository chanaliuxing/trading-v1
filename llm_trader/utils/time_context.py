"""Time context utilities for session awareness

Provides rich time context for LLM to understand:
- Current NY time and session phase
- Minutes remaining until close
- How many decisions left in current session
- When to stop new positions vs flatten all
"""

from datetime import datetime, time, timedelta
from typing import Optional
import pytz

NY = pytz.timezone("America/New_York")


def make_time_ctx(
    ny_now: datetime,
    api_interval_sec: int,
    flatten_at_ny: time,                    # e.g., time(15, 55) for 3:55 PM
    prevent_new_after_ny: Optional[time] = None,  # e.g., time(15, 50) for 3:50 PM
    market_close_ny: time = time(16, 0),    # 4:00 PM
) -> dict:
    """Create rich time context for LLM decision making

    Args:
        ny_now: Current datetime in NY timezone
        api_interval_sec: Seconds between LLM calls (e.g., 20, 60)
        flatten_at_ny: Time to start flattening positions (e.g., 3:55 PM)
        prevent_new_after_ny: Time to stop new positions (e.g., 3:50 PM).
                             If None, uses flatten_at_ny - 5 minutes
        market_close_ny: Market close time (default 4:00 PM)

    Returns:
        Dict with time context fields for prompt builder

    Example:
        >>> from datetime import time
        >>> ny_now = datetime.now(NY)
        >>> ctx = make_time_ctx(
        ...     ny_now=ny_now,
        ...     api_interval_sec=60,
        ...     flatten_at_ny=time(15, 55),
        ...     prevent_new_after_ny=time(15, 50)
        ... )
        >>> ctx.keys()
        dict_keys(['ny_iso', 'session_phase', 'minutes_to_close', 'stop_at_ny',
                   'api_interval_sec', 'decisions_left', 'should_prevent_new_positions',
                   'should_flatten'])
    """
    # Normalize to NY timezone
    ny_now = ny_now.astimezone(NY)
    today = ny_now.date()

    # Build datetimes for today
    flatten_dt = NY.localize(datetime.combine(today, flatten_at_ny))

    if prevent_new_after_ny is None:
        # Default: 5 minutes before flatten
        prevent_dt = flatten_dt - timedelta(minutes=5)
    else:
        prevent_dt = NY.localize(datetime.combine(today, prevent_new_after_ny))

    close_dt = NY.localize(datetime.combine(today, market_close_ny))

    # Calculate decisions_left until flatten time
    seconds_to_flatten = max(0, (flatten_dt - ny_now).total_seconds())
    decisions_left = max(0, int(seconds_to_flatten / max(1, api_interval_sec)))

    # Determine session phase
    open_dt = NY.localize(datetime.combine(today, time(9, 30)))
    if ny_now < open_dt:
        phase = "PREOPEN"
    elif ny_now < open_dt + timedelta(hours=1):
        phase = "OPEN"
    elif ny_now < close_dt - timedelta(hours=1):
        phase = "MIDDAY"
    elif ny_now < close_dt:
        phase = "AFTERNOON"
    else:
        phase = "CLOSE"

    return {
        "ny_iso": ny_now.isoformat(),                        # e.g., "2025-10-21T10:03:44-04:00"
        "session_phase": phase,                               # OPEN/MIDDAY/AFTERNOON/CLOSE
        "minutes_to_close": int((close_dt - ny_now).total_seconds() / 60),
        "stop_at_ny": flatten_dt.strftime("%H:%M:%S"),       # e.g., "15:55:00"
        "api_interval_sec": api_interval_sec,                 # e.g., 60
        "decisions_left": decisions_left,                     # e.g., 45
        "should_prevent_new_positions": ny_now >= prevent_dt,
        "should_flatten": ny_now >= flatten_dt,
    }


def parse_time_string(time_str: str) -> time:
    """Parse time string in HH:MM format

    Args:
        time_str: Time in "HH:MM" or "HH:MM:SS" format

    Returns:
        datetime.time object

    Examples:
        >>> parse_time_string("15:55")
        datetime.time(15, 55)
        >>> parse_time_string("9:30")
        datetime.time(9, 30)
        >>> parse_time_string("15:55:30")
        datetime.time(15, 55, 30)
    """
    parts = time_str.split(":")
    if len(parts) == 2:
        h, m = int(parts[0]), int(parts[1])
        return time(h, m)
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        return time(h, m, s)
    else:
        raise ValueError(f"Invalid time format: {time_str} (expected HH:MM or HH:MM:SS)")


if __name__ == "__main__":
    # Test
    print("Time Context Test")
    print("=" * 60)

    # Simulate 10:30 AM
    test_time = NY.localize(datetime(2025, 10, 24, 10, 30, 0))
    ctx = make_time_ctx(
        ny_now=test_time,
        api_interval_sec=60,
        flatten_at_ny=time(15, 55),
        prevent_new_after_ny=time(15, 50),
    )

    print(f"Test time: {test_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"\nTime context:")
    for key, value in ctx.items():
        print(f"  {key}: {value}")

    print("\n" + "=" * 60)
    print("Testing time string parsing:")
    for test_str in ["9:30", "15:55", "16:00:00"]:
        parsed = parse_time_string(test_str)
        print(f"  '{test_str}' -> {parsed}")
