from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class V47GateConfig:
    atr_rank_skip_max: float = 0.15
    early_override_window_start: dt.time = dt.time(9, 39)
    early_override_window_end: dt.time = dt.time(9, 45)
    early_override_move_mult: float = 0.70
    early_override_vol_min: float = 1.50
    delayed_confirm_end: dt.time = dt.time(10, 0)
    delayed_confirm_move5m_min: float = 0.155
    early_lock_mode: str = "openclose"
    long_drift_second_confirm: bool = False
    long_drift_second_confirm_side: str = "long"
    long_drift_second_confirm_window_start: dt.time = dt.time(9, 45)
    long_drift_second_confirm_window_end: dt.time = dt.time(10, 29)
    long_drift_second_confirm_delay_min: int = 1
    enable_v47_veto: bool = False
    veto_start: dt.time = dt.time(9, 40)
    veto_end: dt.time = dt.time(11, 50)
    veto_rebound_live_atr: float = 0.25
    enable_v47_chase_guard: bool = False
    v47_chase_guard_start: dt.time = dt.time(9, 45)
    v47_chase_guard_end: dt.time = dt.time(11, 50)
    v47_long_pos_day_min: float = 0.80
    v47_short_pos_day_max: float = 0.20
    v47_long_dev_vwap_min: float = 0.0020
    v47_short_dev_vwap_min: float = 0.0020


def early_override_ready(
    *,
    eval_now: dt.datetime,
    side: str,
    atr_eff: float,
    session_open_price: float | None,
    px_0938: float | None,
    close_by_minute: dict[dt.datetime, float],
    vwap_by_minute: dict[dt.datetime, float],
    vol_surge_by_minute: dict[dt.datetime, float],
    config: V47GateConfig,
) -> bool:
    if eval_now.timetz().replace(tzinfo=None) < dt.time(9, 39):
        return False
    if session_open_price is None or px_0938 is None or atr_eff <= 0:
        return False
    if abs(float(px_0938) - float(session_open_price)) < (config.early_override_move_mult * float(atr_eff)):
        return False
    side_sign: int | None = None
    for mm in range(31, 40):
        minute_ts = eval_now.replace(hour=9, minute=mm, second=0, microsecond=0)
        close_v = close_by_minute.get(minute_ts)
        vwap_v = vwap_by_minute.get(minute_ts)
        if close_v is None or vwap_v is None or float(vwap_v) <= 0:
            return False
        sign = 1 if float(close_v) > float(vwap_v) else (-1 if float(close_v) < float(vwap_v) else 0)
        if sign == 0:
            return False
        if side_sign is None:
            side_sign = sign
        elif side_sign != sign:
            return False
    if (side == "LONG" and side_sign != 1) or (side == "SHORT" and side_sign != -1):
        return False
    vol_0938 = vol_surge_by_minute.get(eval_now.replace(hour=9, minute=39, second=0, microsecond=0))
    if vol_0938 is None:
        return False
    return float(vol_0938) >= float(config.early_override_vol_min)


def opening_gate_reason(
    *,
    eval_now: dt.datetime,
    side: str,
    primary_px: float,
    atr_eff: float,
    atr_rank: float | None,
    atr_d: float | None,
    session_open_price: float | None,
    close_by_minute: dict[dt.datetime, float],
    vwap_by_minute: dict[dt.datetime, float],
    vol_surge_by_minute: dict[dt.datetime, float],
    config: V47GateConfig,
    early_override_feature: bool | None = None,
) -> str | None:
    now_t = eval_now.timetz().replace(tzinfo=None)
    in_early_override_window = config.early_override_window_start <= now_t <= config.early_override_window_end
    if early_override_feature is not None:
        early_override = bool(early_override_feature) if in_early_override_window else False
    else:
        early_override = (
            early_override_ready(
                eval_now=eval_now,
                side=side,
                atr_eff=atr_eff,
                session_open_price=session_open_price,
                px_0938=close_by_minute.get(eval_now.replace(hour=9, minute=38, second=0, microsecond=0)),
                close_by_minute=close_by_minute,
                vwap_by_minute=vwap_by_minute,
                vol_surge_by_minute=vol_surge_by_minute,
                config=config,
            )
            if in_early_override_window
            else False
        )
    if (not early_override) and atr_rank is None:
        return "atr_rank_not_ready"
    if (not early_override) and atr_rank is not None and atr_rank <= config.atr_rank_skip_max:
        return "atr_rank_skip"
    if now_t < dt.time(9, 45) and not early_override:
        return "pre_0945_no_early_override"
    if not (dt.time(9, 45) <= now_t <= config.delayed_confirm_end):
        return None
    current_minute = eval_now.replace(second=0, microsecond=0)
    close_now = close_by_minute.get(current_minute)
    close_5m_ago = close_by_minute.get(current_minute - dt.timedelta(minutes=5))
    atr_d_value = atr_d if atr_d is not None else atr_eff
    if close_now is None or close_5m_ago is None or atr_d_value is None or atr_d_value <= 0:
        return "delayed_confirm_not_ready"
    move5m = abs(float(close_now) - float(close_5m_ago)) / max(float(atr_d_value), 1e-9)
    if move5m < float(config.delayed_confirm_move5m_min):
        return "delayed_confirm_not_met"
    if config.early_lock_mode == "openclose" and session_open_price is not None:
        dir_oc = 1 if float(primary_px) > float(session_open_price) else (-1 if float(primary_px) < float(session_open_price) else 0)
        if (side == "LONG" and dir_oc <= 0) or (side == "SHORT" and dir_oc >= 0):
            return "early_lock_openclose_mismatch"
    return None


def veto_reason(
    *,
    eval_now: dt.datetime,
    side: str,
    primary_px: float,
    atr_eff: float,
    live_samples: list[tuple[dt.datetime, float]],
    config: V47GateConfig,
) -> str | None:
    if not config.enable_v47_veto:
        return None
    now_t = eval_now.timetz().replace(tzinfo=None)
    if not (config.veto_start <= now_t <= config.veto_end):
        return None
    ref_30s = live_value_at_or_before(live_samples=live_samples, target_ts=eval_now - dt.timedelta(seconds=30))
    ref_120s = live_value_at_or_before(live_samples=live_samples, target_ts=eval_now - dt.timedelta(seconds=120))
    if ref_30s is not None:
        if side == "LONG" and float(primary_px) < float(ref_30s):
            return "long_rollback_veto"
        if side == "SHORT" and float(primary_px) > float(ref_30s):
            return "short_rollback_veto"
    if ref_120s is not None and atr_eff > 0:
        move_120s_live_atr = (float(primary_px) - float(ref_120s)) / max(float(atr_eff), 1e-9)
        if side == "LONG" and move_120s_live_atr < (-float(config.veto_rebound_live_atr)):
            return "long_rebound_fail_veto"
        if side == "SHORT" and move_120s_live_atr > float(config.veto_rebound_live_atr):
            return "short_rebound_fail_veto"
    return None


def drift_block_reason(
    *,
    eval_now: dt.datetime,
    side: str,
    primary_px: float,
    primary_vwap: float,
    close_by_minute: dict[dt.datetime, float],
    session_pos_day: float | None,
    config: V47GateConfig,
) -> str | None:
    confirm_side_ok = (side == "LONG") if config.long_drift_second_confirm_side == "long" else (side in {"LONG", "SHORT"})
    now_t = eval_now.timetz().replace(tzinfo=None)
    if (
        config.long_drift_second_confirm
        and confirm_side_ok
        and (config.long_drift_second_confirm_window_start <= now_t <= config.long_drift_second_confirm_window_end)
    ):
        current_minute = eval_now.replace(second=0, microsecond=0)
        close_now = close_by_minute.get(current_minute)
        close_prev = close_by_minute.get(current_minute - dt.timedelta(minutes=max(1, int(config.long_drift_second_confirm_delay_min))))
        if close_now is None or close_prev is None or primary_vwap <= 0:
            return "long_drift_second_confirm_missing"
        if side == "LONG":
            confirm_ok = float(close_now) >= float(close_prev) and float(close_now) > float(primary_vwap)
        else:
            confirm_ok = float(close_now) <= float(close_prev) and float(close_now) < float(primary_vwap)
        if not confirm_ok:
            return "long_drift_second_confirm_failed"
    return chase_guard_reason(
        eval_now=eval_now,
        side=side,
        primary_px=primary_px,
        primary_vwap=primary_vwap,
        session_pos_day=session_pos_day,
        config=config,
    )


def chase_guard_reason(
    *,
    eval_now: dt.datetime,
    side: str,
    primary_px: float,
    primary_vwap: float,
    session_pos_day: float | None,
    config: V47GateConfig,
) -> str | None:
    if not config.enable_v47_chase_guard:
        return None
    now_t = eval_now.timetz().replace(tzinfo=None)
    if not (config.v47_chase_guard_start <= now_t <= config.v47_chase_guard_end) or primary_vwap <= 0:
        return None
    if session_pos_day is None:
        return None
    dev_vwap_pct = (float(primary_px) - float(primary_vwap)) / max(float(primary_vwap), 1e-9)
    if side == "LONG" and session_pos_day >= float(config.v47_long_pos_day_min) and dev_vwap_pct >= float(config.v47_long_dev_vwap_min):
        return "v47_long_chase_guard"
    if side == "SHORT" and session_pos_day <= float(config.v47_short_pos_day_max) and dev_vwap_pct <= (-float(config.v47_short_dev_vwap_min)):
        return "v47_short_chase_guard"
    return None


def live_value_at_or_before(*, live_samples: list[tuple[dt.datetime, float]], target_ts: dt.datetime) -> float | None:
    best_dt: dt.datetime | None = None
    best_value: float | None = None
    for row_dt, row_value in live_samples:
        if row_dt > target_ts:
            continue
        if best_dt is None or row_dt > best_dt:
            best_dt = row_dt
            best_value = row_value
    return best_value
