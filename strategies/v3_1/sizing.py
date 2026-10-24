"""Position sizing and risk budget logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .params_store import ParamSet


@dataclass
class SizingResult:
    stop0: float
    r_per_share: float
    shares: int
    risk_budget: float
    max_notional_cap: float


def compute_stop0(
    entry_px: float,
    atr: float,
    swing_low: Optional[float],
    pullback_low: Optional[float],
    lane: str,
    params: ParamSet,
) -> float:
    buffer = params.atr_buffer * atr
    if lane == "LANE_1_TREND":
        stop_atr = entry_px - params.atr_stop_lane1 * atr
        swing = swing_low if swing_low is not None else stop_atr
        return min(stop_atr, swing - buffer)
    stop_atr = entry_px - params.atr_stop_lane2 * atr
    pullback = pullback_low if pullback_low is not None else stop_atr
    return min(stop_atr, pullback - buffer)


def compute_sizing(
    entry_px: float,
    stop0: float,
    equity: float,
    params: ParamSet,
) -> SizingResult:
    r_per_share = max(entry_px - stop0, 0.0)
    risk_budget = max(equity * params.risk_per_trade_pct, 0.0)
    if r_per_share <= 0:
        return SizingResult(stop0, r_per_share, 0, risk_budget, params.max_notional_cap)
    shares = int(risk_budget // r_per_share)
    max_shares_by_notional = int(params.max_notional_cap // max(entry_px, 1e-6))
    shares = max(0, min(shares, max_shares_by_notional))
    return SizingResult(stop0, r_per_share, shares, risk_budget, params.max_notional_cap)
