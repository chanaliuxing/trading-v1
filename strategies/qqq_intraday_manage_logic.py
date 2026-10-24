from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Tp1Decision:
    tp1_qty: int
    updated_trail_high_px: float
    updated_trail_floor_px: float | None


@dataclass(frozen=True)
class TrailingDecision:
    trailing_hit: bool
    updated_trail_high_px: float
    updated_trail_floor_px: float | None


@dataclass(frozen=True)
class AddDecision:
    should_add: bool
    add_fill_est: float | None
    updated_entry_px: float | None


def hard_stop_triggered(*, side: str, stop_px: float | None, last_px: float) -> bool:
    if stop_px is None or float(stop_px) <= 0:
        return False
    return float(last_px) >= float(stop_px) if str(side).upper() == "SHORT" else float(last_px) <= float(stop_px)


def target_hit(*, side: str, target_px: float | None, last_px: float) -> bool:
    if target_px is None or float(target_px) <= 0:
        return False
    return float(last_px) <= float(target_px) if str(side).upper() == "SHORT" else float(last_px) >= float(target_px)


def tp1_exit_decision(
    *,
    side: str,
    live_qty: int,
    last_px: float,
    entry_px: float | None,
    entry_atr_used: float | None,
) -> Tp1Decision | None:
    qty_i = int(max(0, live_qty))
    if qty_i <= 1:
        return None
    tp1_qty = max(1, int(qty_i // 2))
    if tp1_qty >= qty_i:
        tp1_qty = max(1, qty_i - 1)
    trail_floor = None
    atr_entry = float(entry_atr_used or 0.0)
    if atr_entry > 0 and entry_px is not None:
        if str(side).upper() == "SHORT":
            trail_floor = round(float(entry_px) - (0.2 * atr_entry), 2)
        else:
            trail_floor = round(float(entry_px) + (0.2 * atr_entry), 2)
    return Tp1Decision(
        tp1_qty=int(tp1_qty),
        updated_trail_high_px=float(last_px),
        updated_trail_floor_px=trail_floor,
    )


def trailing_stop_decision(
    *,
    side: str,
    last_px: float,
    entry_px: float | None,
    entry_atr_used: float | None,
    existing_trail_high_px: float | None,
    existing_trail_floor_px: float | None,
) -> TrailingDecision | None:
    atr_entry = float(entry_atr_used or 0.0)
    if atr_entry <= 0 or entry_px is None:
        return None
    side_u = str(side).upper()
    trail_high = float(last_px) if existing_trail_high_px is None else (min(float(existing_trail_high_px), float(last_px)) if side_u == "SHORT" else max(float(existing_trail_high_px), float(last_px)))
    if side_u == "SHORT":
        trail_line = float(trail_high) + (0.5 * atr_entry)
        floor_line = float(existing_trail_floor_px or (float(entry_px) - (0.2 * atr_entry)))
        effective_stop = min(floor_line, trail_line)
        trailing_hit = float(last_px) >= effective_stop
    else:
        trail_line = float(trail_high) - (0.5 * atr_entry)
        floor_line = float(existing_trail_floor_px or (float(entry_px) + (0.2 * atr_entry)))
        effective_stop = max(floor_line, trail_line)
        trailing_hit = float(last_px) <= effective_stop
    return TrailingDecision(
        trailing_hit=bool(trailing_hit),
        updated_trail_high_px=float(trail_high),
        updated_trail_floor_px=float(floor_line),
    )


def breakeven_stop_price(*, side: str, entry_px: float | None, last_px: float, enable_breakeven_shield: bool) -> float | None:
    if not enable_breakeven_shield or entry_px is None:
        return None
    unrealized = (float(entry_px) - float(last_px)) if str(side).upper() == "SHORT" else (float(last_px) - float(entry_px))
    if unrealized < 1.0:
        return None
    new_stop = round(float(entry_px) - 0.20, 2) if str(side).upper() == "SHORT" else round(float(entry_px) + 0.20, 2)
    valid_stop = new_stop > float(last_px) + 0.05 if str(side).upper() == "SHORT" else new_stop < float(last_px) - 0.05
    return float(new_stop) if valid_stop else None


def logic_retreat_should_flatten(
    *,
    side: str,
    now: datetime,
    entry_time: datetime | None,
    primary_px: float | None,
    primary_vwap: float | None,
    primary_age_sec: float | None,
    min_hold_sec: float,
    max_primary_age_sec: float,
) -> tuple[bool, bool]:
    if entry_time is None or primary_px is None or primary_vwap is None or float(primary_vwap) <= 0:
        return False, False
    if float(min_hold_sec) > 0 and (now - entry_time).total_seconds() < float(min_hold_sec):
        return False, False
    if primary_age_sec is not None and float(primary_age_sec) > max(1.0, float(max_primary_age_sec)):
        return False, True
    wrong_side = (str(side).upper() == "LONG" and float(primary_px) < float(primary_vwap)) or (
        str(side).upper() == "SHORT" and float(primary_px) > float(primary_vwap)
    )
    return bool(wrong_side), False


def add_position_decision(
    *,
    side: str,
    now: datetime,
    entry_time: datetime | None,
    entry_px: float | None,
    entry_atr_used: float | None,
    last_px: float,
    planned_probe_qty: int,
    planned_add_qty: int,
    add_done: bool,
) -> AddDecision | None:
    if add_done or planned_add_qty <= 0 or entry_px is None or entry_time is None:
        return None
    now_minute = now.replace(second=0, microsecond=0)
    entry_minute = entry_time.replace(second=0, microsecond=0)
    minute_delta = int((now_minute - entry_minute).total_seconds() // 60)
    if minute_delta < 0 or minute_delta > 2:
        return AddDecision(should_add=False, add_fill_est=None, updated_entry_px=None)
    atr_entry = float(entry_atr_used or 0.0)
    if atr_entry <= 0:
        return None
    if str(side).upper() == "SHORT":
        threshold = float(entry_px) - (0.08 * atr_entry)
        trigger = float(last_px) <= threshold
        add_fill_est = min(float(last_px), threshold)
    else:
        threshold = float(entry_px) + (0.08 * atr_entry)
        trigger = float(last_px) >= threshold
        add_fill_est = max(float(last_px), threshold)
    if not trigger:
        return AddDecision(should_add=False, add_fill_est=None, updated_entry_px=None)
    total_qty = int(max(0, planned_probe_qty)) + int(max(0, planned_add_qty))
    updated_entry_px = None
    if planned_probe_qty > 0 and planned_add_qty > 0 and total_qty > 0:
        updated_entry_px = ((float(entry_px) * float(planned_probe_qty)) + (float(add_fill_est) * float(planned_add_qty))) / float(total_qty)
    return AddDecision(
        should_add=True,
        add_fill_est=float(add_fill_est),
        updated_entry_px=updated_entry_px,
    )
