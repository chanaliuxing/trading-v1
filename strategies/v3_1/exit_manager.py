"""Exit management with strict priority chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .schemas import ExitPlan, ExitTarget
from .params_store import ParamSet


@dataclass
class PositionState:
    entry_px: float
    stop0: float
    r_per_share: float
    lane: str
    entry_ts: int
    entry_bar_idx: int = 0
    targets_hit: Dict[str, bool] = field(default_factory=dict)
    max_favorable_px: float = 0.0
    max_adverse_px: float = 0.0


@dataclass
class ExitAction:
    action: str
    price: float
    qty_pct: float
    reason: str


def build_exit_plan(entry_px: float, r_per_share: float, lane: str, targets: Dict[str, float]) -> ExitPlan:
    t1 = ExitTarget("T1", entry_px + 1.5 * r_per_share, 0.5 if lane == "LANE_2_PULLBACK" else 0.3, "+1.5R")
    t2 = ExitTarget("T2", entry_px + 2.5 * r_per_share, 0.3 if lane == "LANE_2_PULLBACK" else 0.4, "+2.5R")
    extra_targets = []
    for k, v in targets.items():
        if v:
            extra_targets.append(ExitTarget(k, v, 0.2, k))
    all_targets = sorted([t1, t2] + extra_targets, key=lambda t: t.price)

    trail = {
        "method": "EMA_MINUS_ATR",
        "ema": "EMA10" if lane == "LANE_2_PULLBACK" else "EMA20",
        "atr_mult": 0.2 if lane == "LANE_2_PULLBACK" else 0.3,
    }

    time_stop = {
        "max_days": 10 if lane == "LANE_2_PULLBACK" else 18,
        "no_progress": {"n_days": 5, "mfe_r": 1.5, "range_atr": 1.0},
    }

    return ExitPlan(
        priority_chain=["STOP", "PROTECT", "TRIM", "TRAIL", "TIME"],
        targets=all_targets,
        gap_rule={"mode": "CUMULATIVE_TRIM"},
        trail=trail,
        time_stop=time_stop,
    )


def evaluate_exits(
    params: ParamSet,
    position: PositionState,
    close_px: float,
    ema10: Optional[float],
    ema20: Optional[float],
    atr: float,
    bars_held: int,
    mfe_r: float,
    range_5d: float,
) -> List[ExitAction]:
    actions: List[ExitAction] = []
    r = position.r_per_share

    # STOP
    if close_px <= position.stop0:
        actions.append(ExitAction("EXIT_ALL", close_px, 1.0, "STOP"))
        return actions

    # PROTECT
    protect_r = 1.0 if position.lane == "LANE_1_TREND" else 0.7
    if r > 0 and (close_px - position.entry_px) / r >= protect_r:
        if position.stop0 < position.entry_px:
            actions.append(ExitAction("MOVE_STOP", position.entry_px, 0.0, "PROTECT"))

    # TRIM targets handled in strategy (gap cumulative)

    # TRAIL
    if position.lane == "LANE_1_TREND":
        if ema20 is not None and close_px < ema20 - params.trail_lane1_atr * atr:
            actions.append(ExitAction("EXIT_RUNNER", close_px, 1.0, "TRAIL"))
    else:
        if ema10 is not None and close_px < ema10 - params.trail_lane2_atr * atr:
            actions.append(ExitAction("EXIT_RUNNER", close_px, 1.0, "TRAIL"))

    # TIME + NoProgress
    no_progress = (
        bars_held >= (params.no_progress_lane2_days if position.lane == "LANE_2_PULLBACK" else params.no_progress_lane1_days)
        and mfe_r < params.no_progress_mfe_r
        and range_5d < params.no_progress_range_atr * atr
    )
    max_days = params.max_days_lane2 if position.lane == "LANE_2_PULLBACK" else params.max_days_lane1
    if bars_held >= max_days and no_progress:
        actions.append(ExitAction("EXIT_ALL", close_px, 1.0, "TIME"))

    return actions
