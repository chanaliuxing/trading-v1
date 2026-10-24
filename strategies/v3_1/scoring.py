"""Quality scoring for V3.1 strategy."""

from __future__ import annotations

from typing import Dict, Optional

from .params_store import ParamSet


def _clip(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def trend_strength(features: Dict[str, float]) -> float:
    close = features.get("close")
    ema20 = features.get("ema20") or features.get("ema_20")
    ema50 = features.get("ema50") or features.get("ema_50")
    ema20_slope = features.get("ema20_slope")
    if close is None or ema20 is None:
        return 0.0
    if ema50 is not None and ema20_slope is not None:
        if close > ema50 and ema20 > ema50 and ema20_slope > 0:
            return 24.0
    if close > ema20:
        return 15.0
    return 6.0


def entry_quality_breakout(features: Dict[str, float], vol_mult: Optional[float]) -> float:
    high = features.get("high")
    low = features.get("low")
    close = features.get("close")
    open_px = features.get("open")
    if None in (high, low, close, open_px):
        return 0.0
    rng = max(high - low, 1e-6)
    body_pct = abs(close - open_px) / rng
    close_pos = (close - low) / rng
    vol_mult = vol_mult or 0.0
    score = 10 * _clip(body_pct, 0, 1) + 10 * _clip(close_pos, 0, 1) + 5 * _clip(vol_mult / 2.0, 0, 1)
    return _clip(score, 0, 25)


def entry_quality_pullback(
    features: Dict[str, float],
    anchor: float,
    atr: float,
    bars_since_touch: int,
) -> float:
    high = features.get("high")
    low = features.get("low")
    close = features.get("close")
    open_px = features.get("open")
    if None in (high, low, close, open_px) or atr <= 0:
        return 0.0
    depth = max(0.0, (anchor - low) / atr)
    depth_score = 10 * _clip(1 - depth / 0.8, 0, 1)
    if bars_since_touch <= 1:
        reclaim = 10
    elif bars_since_touch <= 2:
        reclaim = 8
    elif bars_since_touch <= 4:
        reclaim = 5
    else:
        reclaim = 2
    rng = max(high - low, 1e-6)
    lower_wick = (min(open_px, close) - low) / rng
    wick_score = 5 * _clip(lower_wick / 0.5, 0, 1)
    score = depth_score + reclaim + wick_score
    return _clip(score, 0, 25)


def structure_space(wall_dist: Optional[float], params: ParamSet) -> float:
    if wall_dist is None:
        return 10.0
    if wall_dist >= params.wall_dist_hi:
        return 25.0
    if wall_dist <= params.wall_dist_lo:
        return 6.0
    span = params.wall_dist_hi - params.wall_dist_lo
    if span <= 0:
        return 10.0
    frac = (wall_dist - params.wall_dist_lo) / span
    return 15.0 + 5.0 * _clip(frac, 0, 1)


def options_risk(ivp: Optional[float], atr_pct_rank: Optional[float]) -> float:
    value = ivp if ivp is not None else atr_pct_rank
    if value is None:
        return 12.0
    if value < 30:
        return 25.0
    if value > 70:
        return 0.0
    # Linear between 30 and 70
    return 20.0 - (value - 30) * (8.0 / 40.0)


def quality_score(
    trend: float,
    entry: float,
    structure: float,
    options: float,
) -> float:
    return _clip(trend + entry + structure + options, 0, 100)


def grade_from_score(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    return "D"
