from __future__ import annotations

from datetime import time
from typing import Any, Dict, Tuple


def _parse_hms(raw: Any, default: time) -> time:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        hh, mm, ss = [int(part) for part in text.split(":")]
        return time(hh, mm, ss)
    except Exception:
        return default


def resolve_window(entry_model: str, extras: Dict[str, Any]) -> Tuple[time, time]:
    model = str(entry_model or "baseline").strip().lower()
    elite_ab_dual_mode = bool(extras.get("elite_ab_dual_mode", False))
    if model == "v47_fast_drift":
        start_t = _parse_hms(extras.get("ab_time_start", "09:30:00"), time(9, 30))
        end_t = _parse_hms(extras.get("ab_time_end", "14:00:00"), time(14, 0))
        if end_t < time(14, 30):
            end_t = time(14, 30)
        return start_t, end_t
    if model in {"v46_ab"} or (model == "v44_elite" and elite_ab_dual_mode):
        start_t = _parse_hms(extras.get("ab_time_start", "09:32:00"), time(9, 32))
        end_t = _parse_hms(extras.get("ab_time_end", "10:30:00"), time(10, 30))
        return start_t, end_t
    return time(9, 45), time(15, 30)


def in_window(now_t: time, *, start_t: time, end_t: time) -> bool:
    return start_t <= now_t <= end_t
