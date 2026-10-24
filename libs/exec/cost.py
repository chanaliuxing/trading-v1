"""
Unified cost model helpers (spread, slippage, fees) for strategy simulations.

These functions compute conservative fill estimates to be attached in decision
extras for observability and A/B analysis. They DO NOT modify actual orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Dict


Side = Literal["BUY", "SELL"]


@dataclass
class CostParams:
    tick_size: float = 0.01
    gamma_spread: float = 0.10  # fraction of (H-L)
    slippage_bps_per_atr: float = 0.0  # if >0, slip = bps*ATR_basis
    fee_per_share: float = 0.0035


def estimate_spread(high: float, low: float, tick: float, gamma: float) -> float:
    rng = max(0.0, float(high) - float(low))
    est = max(float(tick), float(gamma) * rng)
    return round(est, 4)


def estimate_slip(atr_basis: float, slippage_bps_per_atr: float) -> float:
    if atr_basis <= 0 or slippage_bps_per_atr <= 0:
        return 0.0
    # slip = (bps * ATR); bps here is a fraction (e.g., 0.02..0.06)
    return round(float(slippage_bps_per_atr) * float(atr_basis), 4)


def entry_fill_est(
    side: Side,
    trigger_px: float,
    atr_basis: float,
    params: CostParams,
    bar_high: float | None = None,
    bar_low: float | None = None,
) -> Dict[str, float]:
    """Estimate entry fill price and components.

    ENTRY: fill = trigger + spread/2 + slip (BUY) or trigger - spread/2 - slip (SELL)
    """
    spread = estimate_spread(bar_high or trigger_px, bar_low or trigger_px, params.tick_size, params.gamma_spread)
    slip = estimate_slip(atr_basis, params.slippage_bps_per_atr)
    mid_adj = (spread / 2.0) + slip
    if side == "BUY":
        fill = trigger_px + mid_adj
    else:
        fill = trigger_px - mid_adj
    return {
        "fill": round(fill, 4),
        "spread": spread,
        "slip": round(slip, 4),
        "fee_per_share": params.fee_per_share,
    }


def exit_fill_est(
    side: Side,
    target_px: float,
    atr_basis: float,
    params: CostParams,
    bar_high: float | None = None,
    bar_low: float | None = None,
    reason: str = "",
) -> Dict[str, float]:
    """Estimate exit fill price and components for SL/TP/TRAIL/FLAT.

    SL:    fill = SL − spread/2 − slip (SELL from long)
    TP/TR: fill = target + spread/2 + slip (SELL from long)
    FLAT:  treated as TP/TR (conservative)
    """
    spread = estimate_spread(bar_high or target_px, bar_low or target_px, params.tick_size, params.gamma_spread)
    slip = estimate_slip(atr_basis, params.slippage_bps_per_atr)
    mid_adj = (spread / 2.0) + slip
    if reason.upper() == "SL":
        # more conservative to the downside for exit
        fill = target_px - mid_adj if side == "SELL" else target_px + mid_adj
    else:
        fill = target_px + mid_adj if side == "SELL" else target_px - mid_adj
    return {
        "fill": round(fill, 4),
        "spread": spread,
        "slip": round(slip, 4),
        "fee_per_share": params.fee_per_share,
    }

