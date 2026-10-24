from __future__ import annotations

from datetime import time, timedelta
from typing import Optional

from strategies.qqq_intraday_v47_logic import V47GateConfig, opening_gate_reason, veto_reason

from .contracts import DirectionDecision, EntryContext


def _minute_close(ctx: EntryContext, minutes_ago: int) -> Optional[float]:
    """Return the 1-minute bar close N minutes before ctx.now, using close_by_minute."""
    target = ctx.now.replace(second=0, microsecond=0) - timedelta(minutes=minutes_ago)
    return ctx.close_by_minute.get(target)


def _direction_from_vwap(ctx: EntryContext) -> DirectionDecision:
    if ctx.qqq_now > ctx.vwap_now:
        return DirectionDecision("LONG", "qqq_above_vwap")
    if ctx.qqq_now < ctx.vwap_now:
        return DirectionDecision("SHORT", "qqq_below_vwap")
    return DirectionDecision("NONE", "direction_mismatch", {"qqq_now": ctx.qqq_now, "vwap_now": ctx.vwap_now})


def baseline_direction(ctx: EntryContext) -> DirectionDecision:
    base = _direction_from_vwap(ctx)
    if base.direction == "NONE":
        return base
    qqq_only = bool(ctx.extras.get("qqew_decoupled", ctx.qqew_decoupled))
    if not qqq_only and (ctx.qqew_now is not None) and (ctx.qqew_vwap_now is not None) and ctx.qqew_vwap_now > 0:
        if base.direction == "LONG" and not (ctx.qqew_now > ctx.qqew_vwap_now):
            return DirectionDecision("NONE", "direction_mismatch", {"qqew_now": ctx.qqew_now, "qqew_vwap_now": ctx.qqew_vwap_now})
        if base.direction == "SHORT" and not (ctx.qqew_now < ctx.qqew_vwap_now):
            return DirectionDecision("NONE", "direction_mismatch", {"qqew_now": ctx.qqew_now, "qqew_vwap_now": ctx.qqew_vwap_now})

    v3m_abs = abs(float(ctx.v3m_signed))
    vol_pulse_on = float(ctx.extras.get("vol_pulse_on", 1.6))
    k_normal = float(ctx.extras.get("k_normal", 0.40))
    k_pulse = float(ctx.extras.get("k_pulse", 0.30))
    tv_min_usd = float(ctx.extras.get("tv_min_usd", 0.20))
    pulse = ctx.vol_surge_ratio >= vol_pulse_on
    threshold = max((k_pulse if pulse else k_normal) * ctx.atr_eff, tv_min_usd)
    if v3m_abs < threshold:
        return DirectionDecision("NONE", "velocity_too_low", {"v3m_abs": v3m_abs, "tv": threshold, "pulse": pulse})
    return DirectionDecision(base.direction, "direction_allowed", {"v3m_abs": v3m_abs, "tv": threshold, "pulse": pulse})


def fast_drift_direction(ctx: EntryContext) -> DirectionDecision:
    base = _direction_from_vwap(ctx)
    if base.direction == "NONE":
        return base

    cfg = V47GateConfig(
        atr_rank_skip_max=float(ctx.extras.get("atr_rank_skip_max", 0.15)),
        early_override_window_start=time.fromisoformat(str(ctx.extras.get("early_override_window_start", "09:39:00"))),
        early_override_window_end=time.fromisoformat(str(ctx.extras.get("early_override_window_end", "09:45:00"))),
        early_override_move_mult=float(ctx.extras.get("early_override_move_mult", 0.70)),
        early_override_vol_min=float(ctx.extras.get("early_override_vol_min", 1.50)),
        delayed_confirm_end=time.fromisoformat(str(ctx.extras.get("delayed_confirm_end", "10:00:00"))),
        delayed_confirm_move5m_min=float(ctx.extras.get("delayed_confirm_move5m_min", 0.155)),
        early_lock_mode=str(ctx.extras.get("early_lock_mode", "openclose")).strip().lower(),
        enable_v47_veto=bool(ctx.extras.get("enable_v47_veto", False)),
        veto_start=time.fromisoformat(str(ctx.extras.get("veto_start", "09:40:00"))),
        veto_end=time.fromisoformat(str(ctx.extras.get("veto_end", "11:50:00"))),
        veto_rebound_live_atr=float(ctx.extras.get("veto_rebound_live_atr", 0.25)),
        enable_v47_chase_guard=bool(ctx.extras.get("enable_v47_chase_guard", False)),
        v47_chase_guard_start=time.fromisoformat(str(ctx.extras.get("v47_chase_guard_start", "09:45:00"))),
        v47_chase_guard_end=time.fromisoformat(str(ctx.extras.get("v47_chase_guard_end", "11:50:00"))),
        v47_long_pos_day_min=float(ctx.extras.get("v47_long_pos_day_min", 0.80)),
        v47_short_pos_day_max=float(ctx.extras.get("v47_short_pos_day_max", 0.20)),
        v47_long_dev_vwap_min=float(ctx.extras.get("v47_long_dev_vwap_min", 0.0020)),
        v47_short_dev_vwap_min=float(ctx.extras.get("v47_short_dev_vwap_min", 0.0020)),
    )
    atr_rank = ctx.extras.get("atr_rank_today", ctx.extras.get("atr_rank_prev_day"))
    atr_d = ctx.extras.get("atr_d_override", ctx.extras.get("atr_d_prev_day"))
    open_reason = opening_gate_reason(
        eval_now=ctx.now,
        side=base.direction,
        primary_px=ctx.qqq_now,
        atr_eff=ctx.atr_eff,
        atr_rank=float(atr_rank) if atr_rank is not None else None,
        atr_d=float(atr_d) if atr_d is not None else None,
        session_open_price=ctx.session_open_price,
        close_by_minute=ctx.close_by_minute,
        vwap_by_minute=ctx.vwap_by_minute,
        vol_surge_by_minute=ctx.vol_surge_by_minute,
        config=cfg,
    )
    if open_reason is not None:
        return DirectionDecision("NONE", open_reason)

    deadzone = float(ctx.extras.get("momentum_deadzone", 0.05))
    sign_ok = (base.direction == "LONG" and ctx.v3m_signed > deadzone) or (base.direction == "SHORT" and ctx.v3m_signed < -deadzone)
    if not sign_ok:
        return DirectionDecision("NONE", "sign_mismatch", {"v3m_signed": ctx.v3m_signed, "deadzone": deadzone})

    # --- FAST mode (optional, disabled by default) ---
    fast_enabled = bool(ctx.extras.get("enable_fast", False))
    fast_v3m_atr_min = float(ctx.extras.get("fast_v3m_atr_min", 0.60))
    fast_vol_min = float(ctx.extras.get("fast_vol_min", 999.0))
    v3m_atr = float(ctx.v3m_signed) / max(float(ctx.atr_eff), 1e-9)
    fast_ok = (
        fast_enabled
        and sign_ok
        and abs(v3m_atr) >= fast_v3m_atr_min
        and ctx.vol_surge_ratio >= fast_vol_min
    )

    # --- DRIFT mode: 3 consecutive 1-minute bars all moving in direction,
    #     plus cumulative 3-minute move >= drift_cum_atr_min * atr_eff.
    #     This is the exact same logic as the stock strategy (lines 4080-4097). ---
    drift_bar_min_atr = float(ctx.extras.get("drift_bar_min_atr", 0.10))
    drift_cum_atr_min = float(ctx.extras.get("drift_cum_atr_min", 0.45))
    c0 = _minute_close(ctx, 0)
    c1 = _minute_close(ctx, 1)
    c2 = _minute_close(ctx, 2)
    c3 = _minute_close(ctx, 3)
    drift_ok = False
    if c0 is not None and c1 is not None and c2 is not None and c3 is not None:
        bar_th = drift_bar_min_atr * float(ctx.atr_eff)
        d1 = float(c0 - c1)
        d2 = float(c1 - c2)
        d3 = float(c2 - c3)
        cum = float(c0 - c3)
        if base.direction == "LONG":
            drift_ok = (d1 >= bar_th and d2 >= bar_th and d3 >= bar_th and cum >= drift_cum_atr_min * float(ctx.atr_eff))
        else:
            drift_ok = (-d1 >= bar_th and -d2 >= bar_th and -d3 >= bar_th and -cum >= drift_cum_atr_min * float(ctx.atr_eff))

    if fast_ok:
        mode = "FAST"
        tv = fast_v3m_atr_min
    elif drift_ok:
        mode = "DRIFT"
        tv = drift_cum_atr_min
    else:
        # Neither FAST nor DRIFT pattern detected.
        return DirectionDecision(
            "NONE",
            "fast_drift_not_ready",
            {
                "v3m_signed": ctx.v3m_signed,
                "v3m_atr": round(v3m_atr, 4),
                "drift_cum_atr_min": drift_cum_atr_min,
                "c0": c0,
                "c1": c1,
                "c2": c2,
                "c3": c3,
                "atr_eff": ctx.atr_eff,
            },
        )

    drift_reason = veto_reason(
        eval_now=ctx.now,
        side=base.direction,
        primary_px=ctx.qqq_now,
        atr_eff=ctx.atr_eff,
        live_samples=list(ctx.live_samples),
        config=cfg,
    )
    if drift_reason is not None:
        return DirectionDecision("NONE", drift_reason)
    return DirectionDecision(
        base.direction,
        "direction_allowed",
        {"mode": mode, "tv": tv, "v3m_signed": ctx.v3m_signed, "v3m_atr": round(v3m_atr, 4)},
    )


def elite_direction(ctx: EntryContext) -> DirectionDecision:
    base = _direction_from_vwap(ctx)
    if base.direction == "NONE":
        return base
    max_ext = float(ctx.extras.get("elite_max_ext_mult", 1.30)) * ctx.atr_eff
    if abs(ctx.qqq_now - ctx.vwap_now) > max_ext:
        return DirectionDecision("NONE", "elite_overextended", {"max_ext": max_ext})

    deadzone = float(ctx.extras.get("elite_deadzone_mult", 0.15)) * ctx.atr_eff
    if base.direction == "LONG" and ctx.v3m_signed <= deadzone:
        return DirectionDecision("NONE", "elite_sign_mismatch", {"v3m_signed": ctx.v3m_signed, "deadzone": deadzone})
    if base.direction == "SHORT" and ctx.v3m_signed >= -deadzone:
        return DirectionDecision("NONE", "elite_sign_mismatch", {"v3m_signed": ctx.v3m_signed, "deadzone": deadzone})

    v3m_min = float(ctx.extras.get("elite_v3m_min_mult", 0.35)) * ctx.atr_eff
    if abs(ctx.v3m_signed) < v3m_min:
        return DirectionDecision("NONE", "velocity_too_low", {"v3m_abs": abs(ctx.v3m_signed), "tv": v3m_min})

    if ctx.vol_surge_ratio < float(ctx.extras.get("elite_vol_ratio_min", 1.35)):
        return DirectionDecision("NONE", "elite_volume_too_low", {"vol_surge_ratio": ctx.vol_surge_ratio})

    if (not ctx.qqew_decoupled) and (ctx.qqew_now is not None) and (ctx.qqew_vwap_now is not None) and ctx.qqew_vwap_now > 0:
        if base.direction == "LONG" and not (ctx.qqew_now > ctx.qqew_vwap_now):
            return DirectionDecision("NONE", "direction_mismatch", {"qqew_now": ctx.qqew_now, "qqew_vwap_now": ctx.qqew_vwap_now})
        if base.direction == "SHORT" and not (ctx.qqew_now < ctx.qqew_vwap_now):
            return DirectionDecision("NONE", "direction_mismatch", {"qqew_now": ctx.qqew_now, "qqew_vwap_now": ctx.qqew_vwap_now})
    return DirectionDecision(base.direction, "direction_allowed", {"mode": "ELITE"})


def ab_direction(ctx: EntryContext) -> DirectionDecision:
    base = _direction_from_vwap(ctx)
    if base.direction == "NONE":
        return base
    deadzone = float(ctx.extras.get("elite_deadzone_mult", 0.15)) * float(ctx.atr_eff)
    v3m_min = float(ctx.extras.get("elite_v3m_min_mult", 0.35)) * float(ctx.atr_eff)
    opening_breakout_vol_min = float(ctx.extras.get("opening_breakout_vol_min", 1.20))
    opening_breakout_ext_mult = float(ctx.extras.get("opening_breakout_ext_mult", 1.60))
    is_opening_mode = ctx.now.time() < time(9, 40)

    if base.direction == "LONG" and ctx.v3m_signed <= deadzone:
        return DirectionDecision("NONE", "ab_opening_sign_mismatch" if is_opening_mode else "ab_a_sign_mismatch", {"v3m_signed": ctx.v3m_signed, "deadzone": deadzone})
    if base.direction == "SHORT" and ctx.v3m_signed >= -deadzone:
        return DirectionDecision("NONE", "ab_opening_sign_mismatch" if is_opening_mode else "ab_a_sign_mismatch", {"v3m_signed": ctx.v3m_signed, "deadzone": deadzone})
    if abs(ctx.v3m_signed) < v3m_min:
        return DirectionDecision("NONE", "ab_opening_velocity_too_low" if is_opening_mode else "ab_a_velocity_too_low", {"v3m_abs": abs(ctx.v3m_signed), "tv": v3m_min})
    if is_opening_mode and ctx.vol_surge_ratio < opening_breakout_vol_min:
        return DirectionDecision("NONE", "ab_opening_volume_too_low", {"vol_surge_ratio": ctx.vol_surge_ratio, "tv": opening_breakout_vol_min})
    if is_opening_mode:
        ext_b = opening_breakout_ext_mult * float(ctx.atr_eff)
        if abs(float(ctx.qqq_now - ctx.vwap_now)) > ext_b:
            return DirectionDecision("NONE", "ab_opening_overextended", {"distance_abs": abs(float(ctx.qqq_now - ctx.vwap_now)), "tv": ext_b})
    return DirectionDecision(base.direction, "direction_allowed", {"mode": "ELITE_AB"})
