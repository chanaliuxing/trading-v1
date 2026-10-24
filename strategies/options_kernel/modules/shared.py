from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, Optional

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        if parsed != parsed:
            return float(default)
        return parsed
    except Exception:
        return float(default)


def minute_floor(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def parse_hms(value: Any, default: time) -> time:
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        hh, mm, ss = [int(part) for part in raw.split(":")]
        return time(hh, mm, ss)
    except Exception:
        return default


def is_in_split_entry_window(now: datetime, extras: Dict[str, Any]) -> bool:
    now_t = now.time()
    am_start = parse_hms(
        extras.get("entry_start_am", "09:40:00"),
        parse_hms(extras.get("ab_time_start", "09:40:00"), time(9, 40)),
    )
    am_end = parse_hms(extras.get("entry_end_am", "12:00:00"), time(12, 0))
    pm_start = parse_hms(extras.get("entry_start_pm", "13:00:00"), time(13, 0))
    pm_end = parse_hms(
        extras.get("entry_end_pm", "15:00:00"),
        parse_hms(extras.get("ab_time_end", "15:00:00"), time(15, 0)),
    )
    no_new_after = parse_hms(extras.get("no_new_entry_after", "15:00:00"), time(15, 0))
    if now_t > no_new_after:
        return False
    return bool((am_start <= now_t <= am_end) or (pm_start <= now_t <= pm_end))


def bars_since_setup(now: datetime, setup_ts: datetime) -> int:
    try:
        now_minute = minute_floor(now)
        setup_minute = minute_floor(setup_ts)
        delta_sec = (now_minute - setup_minute).total_seconds()
        return max(0, int(round(delta_sec / 60.0)))
    except Exception:
        return 0


def find_entry_index(history: list[Dict[str, Any]], opened_at: datetime) -> Optional[int]:
    for idx, bar in enumerate(history):
        ts = bar.get("ts")
        if isinstance(ts, datetime) and ts >= opened_at:
            return idx
    return None


def best_fav_stats(
    history: list[Dict[str, Any]],
    *,
    entry_idx: int,
    side: str,
    entry_underlying: float,
    r_per_share: float,
) -> tuple[float, int]:
    best_r = 0.0
    best_idx = entry_idx
    denom = max(0.01, float(r_per_share or 0.0))
    for idx in range(entry_idx, len(history)):
        row = history[idx]
        high_px = safe_float(row.get("high"))
        low_px = safe_float(row.get("low"))
        fav_move_r = (high_px - entry_underlying) / denom if side == "CALL" else (entry_underlying - low_px) / denom
        if fav_move_r > best_r:
            best_r = float(fav_move_r)
            best_idx = idx
    return best_r, best_idx
