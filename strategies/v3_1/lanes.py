"""Lane definitions for V3.1 strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .indicators import Bar


@dataclass
class LaneSignal:
    lane: str
    entry_type: str
    trigger_px: float
    anchor_px: float
    pullback_low: float
    pullback_high: float
    bars_since_touch: int


def lane1_breakout(features: Dict[str, float], hhv20: Optional[float]) -> Optional[LaneSignal]:
    close = features.get("close")
    if close is None or hhv20 is None:
        return None
    if close <= hhv20:
        return None
    return LaneSignal(
        lane="LANE_1_TREND",
        entry_type="BREAKOUT",
        trigger_px=close,
        anchor_px=hhv20,
        pullback_low=features.get("low", close),
        pullback_high=features.get("high", close),
        bars_since_touch=0,
    )


def lane1_pullback(
    features: Dict[str, float],
    anchor: float,
    pullback_low: float,
    pullback_high: float,
    bars_since_touch: int,
) -> Optional[LaneSignal]:
    close = features.get("close")
    if close is None:
        return None
    if close <= anchor:
        return None
    if close <= pullback_high:
        return None
    return LaneSignal(
        lane="LANE_1_TREND",
        entry_type="PULLBACK_CONFIRM",
        trigger_px=close,
        anchor_px=anchor,
        pullback_low=pullback_low,
        pullback_high=pullback_high,
        bars_since_touch=bars_since_touch,
    )


def lane2_pullback(
    features: Dict[str, float],
    anchor: float,
    pullback_low: float,
    pullback_high: float,
    bars_since_touch: int,
) -> Optional[LaneSignal]:
    close = features.get("close")
    if close is None:
        return None
    if close <= anchor:
        return None
    if close <= pullback_high:
        return None
    return LaneSignal(
        lane="LANE_2_PULLBACK",
        entry_type="PULLBACK_CONFIRM",
        trigger_px=close,
        anchor_px=anchor,
        pullback_low=pullback_low,
        pullback_high=pullback_high,
        bars_since_touch=bars_since_touch,
    )
