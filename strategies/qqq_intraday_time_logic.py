from __future__ import annotations

import datetime as _dt
from datetime import datetime
from typing import Any, Callable, Dict


def coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, _dt.datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_hms_tuple(value: Any, *, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    parts = raw.split(":")
    if len(parts) < 2:
        return fallback
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2]) if len(parts) >= 3 else 0
    except Exception:
        return fallback
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return fallback
    return hh, mm, ss


def feature_now_et(
    features: Dict[str, Any],
    *,
    tz: Any,
    coerce_datetime_fn: Callable[[Any], datetime | None] = coerce_datetime,
    parse_hms_tuple_fn: Callable[[Any], tuple[int, int, int]] | None = None,
    now_fn: Callable[[Any], datetime] = datetime.now,
) -> datetime:
    for key in ("now_et", "feature_time_et"):
        parsed = coerce_datetime_fn(features.get(key))
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
    raw_time = str(features.get("now_time_et") or "").strip()
    if raw_time:
        try:
            parser = parse_hms_tuple_fn or (lambda v: parse_hms_tuple(v, fallback=(0, 0, 0)))
            hh, mm, ss = parser(raw_time)
            now = now_fn(tz)
            return now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        except Exception:
            pass
    return now_fn(tz)
