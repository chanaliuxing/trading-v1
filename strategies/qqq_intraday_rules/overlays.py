from __future__ import annotations

from datetime import time, timedelta

from .contracts import DirectionDecision, EntryContext


def extreme_overlay(ctx: EntryContext, decision: DirectionDecision) -> DirectionDecision:
    if not bool(ctx.extras.get("entry_extreme_filter_enabled", False)):
        return decision
    dist_min = float(ctx.extras.get("entry_extreme_distance_from_vwap_atr_min", 8.1))
    v3m_signed_min = float(ctx.extras.get("entry_extreme_v3m_signed_min", 0.64))
    seed_n_min = int(ctx.extras.get("entry_extreme_seed_n_min", 700))
    seed_n = int(ctx.extras.get("seed_n", 0))
    if (
        ctx.distance_from_vwap_atr > dist_min
        and float(ctx.v3m_signed) > v3m_signed_min
        and seed_n > seed_n_min
    ):
        return DirectionDecision("NONE", "extreme_filter_block", {"distance_from_vwap_atr": ctx.distance_from_vwap_atr, "tv": dist_min})
    return decision


extreme_overlay = extreme_overlay


def anti_chase_overlay(ctx: EntryContext, decision: DirectionDecision) -> DirectionDecision:
    enabled = bool(ctx.extras.get("anti_chase_enabled", False))
    if not enabled:
        return decision
    rank15 = ctx.extras.get("anti_chase_rank15")
    tv = float(ctx.extras.get("anti_chase_distance_from_vwap_atr_max", 3.5))
    if rank15 is not None and ctx.distance_from_vwap_atr >= tv:
        return DirectionDecision("NONE", "anti_chase_blocked", {"rank15": rank15, "tv": tv, "distance_from_vwap_atr": ctx.distance_from_vwap_atr})
    return decision


def v47_chase_guard_overlay(ctx: EntryContext, decision: DirectionDecision) -> DirectionDecision:
    if decision.direction == "NONE" or not bool(ctx.extras.get("enable_v47_chase_guard", False)):
        return decision
    try:
        start_t = time.fromisoformat(str(ctx.extras.get("v47_chase_guard_start", "09:45:00")))
    except Exception:
        start_t = time(9, 45)
    try:
        end_t = time.fromisoformat(str(ctx.extras.get("v47_chase_guard_end", "11:50:00")))
    except Exception:
        end_t = time(11, 50)
    if not (start_t <= ctx.now.time() <= end_t):
        return decision
    if ctx.day_position_rank is None or ctx.vwap_now <= 0:
        return decision
    dev_vwap_pct = (float(ctx.qqq_now) - float(ctx.vwap_now)) / max(float(ctx.vwap_now), 1e-9)
    long_pos_day_min = float(ctx.extras.get("v47_long_pos_day_min", 0.80))
    short_pos_day_max = float(ctx.extras.get("v47_short_pos_day_max", 0.20))
    long_dev_vwap_min = float(ctx.extras.get("v47_long_dev_vwap_min", 0.0020))
    short_dev_vwap_min = float(ctx.extras.get("v47_short_dev_vwap_min", 0.0020))
    if decision.direction == "LONG" and float(ctx.day_position_rank) >= long_pos_day_min and dev_vwap_pct >= long_dev_vwap_min:
        return DirectionDecision(
            "NONE",
            "v47_long_chase_guard",
            {"pos_day": float(ctx.day_position_rank), "dev_vwap_pct": float(dev_vwap_pct), "tv": long_dev_vwap_min},
        )
    if decision.direction == "SHORT" and float(ctx.day_position_rank) <= short_pos_day_max and dev_vwap_pct <= -short_dev_vwap_min:
        return DirectionDecision(
            "NONE",
            "v47_short_chase_guard",
            {"pos_day": float(ctx.day_position_rank), "dev_vwap_pct": float(dev_vwap_pct), "tv": short_dev_vwap_min},
        )
    return decision


def reentry_cooldown_overlay(ctx: EntryContext, decision: DirectionDecision) -> DirectionDecision:
    cooldown_min = float(ctx.extras.get("stop_reentry_cooldown_min", 0.0) or 0.0)
    if cooldown_min <= 0:
        return decision
    last_stop_side = str(ctx.extras.get("last_stop_exit_side", "")).strip().upper()
    last_stop_ts = ctx.extras.get("last_stop_exit_at")
    if last_stop_ts is None or decision.direction == "NONE":
        return decision
    desired = "LONG" if decision.direction == "LONG" else "SHORT"
    if last_stop_side != desired:
        return decision
    try:
        if (ctx.now - last_stop_ts) < timedelta(minutes=cooldown_min):
            return DirectionDecision("NONE", "stop_reentry_cooldown", {"cooldown_min": cooldown_min})
    except Exception:
        return decision
    return decision


def high_vol_confirm_veto(ctx: EntryContext, decision: DirectionDecision) -> DirectionDecision:
    if decision.direction == "NONE":
        return decision
    enabled = bool(ctx.extras.get("enable_high_vol_guard", True))
    if not enabled:
        return decision
    high_vol_ratio = float(ctx.extras.get("high_vol_ratio", 2.2))
    hv_min_vol_surge = float(ctx.extras.get("hv_min_vol_surge", 1.2))
    hv_k = float(ctx.extras.get("hv_k", 0.50))
    hv_tv_min = float(ctx.extras.get("hv_tv_min_usd", 0.60))
    atr_seed = max(float(ctx.atr_seed), 1e-9)
    try:
        override = ctx.extras.get("vol_ratio_override")
        vol_ratio = float(override) if override is not None else float(ctx.atr_live / atr_seed)
    except Exception:
        vol_ratio = float(ctx.atr_live / atr_seed)
    if vol_ratio < high_vol_ratio:
        return decision
    if ctx.vol_surge_ratio < hv_min_vol_surge:
        return DirectionDecision(
            "NONE",
            "high_vol_no_volume",
            {"vol_ratio": vol_ratio, "vol_surge_ratio": ctx.vol_surge_ratio, "tv": hv_min_vol_surge},
        )
    v3m_abs = abs(float(ctx.v3m_signed))
    tv_hv = max(hv_k * float(ctx.atr_eff), hv_tv_min)
    if v3m_abs < tv_hv:
        return DirectionDecision(
            "NONE",
            "high_vol_protection",
            {"vol_ratio": vol_ratio, "v3m_abs": v3m_abs, "tv": tv_hv},
        )
    return decision


def day_shape_overlay(ctx: EntryContext, decision: DirectionDecision) -> DirectionDecision:
    if decision.direction == "NONE":
        return decision
    enabled = bool(ctx.extras.get("day_shape_overlay_enabled", False))
    if not enabled:
        return decision
    rank = ctx.day_position_rank
    if rank is None:
        return decision
    long_rank_max = float(ctx.extras.get("day_shape_long_rank_max", 0.95))
    short_rank_min = float(ctx.extras.get("day_shape_short_rank_min", 0.05))
    if decision.direction == "LONG" and float(rank) >= long_rank_max:
        return DirectionDecision("NONE", "day_shape_long_exhaustion", {"day_position_rank": rank, "tv": long_rank_max})
    if decision.direction == "SHORT" and float(rank) <= short_rank_min:
        return DirectionDecision("NONE", "day_shape_short_exhaustion", {"day_position_rank": rank, "tv": short_rank_min})
    return decision
