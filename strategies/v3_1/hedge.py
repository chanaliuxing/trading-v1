"""Hedge decision logic for V3.1 strategy."""

from __future__ import annotations

from .schemas import HedgeAction


def hedge_action(grade: str, hedge_cost_ratio: float, threshold: float) -> HedgeAction:
    if hedge_cost_ratio > threshold:
        return HedgeAction(
            required=False,
            method="SIZE_REDUCTION",
            size_reduction_pct=_size_reduction_for_grade(grade),
            alt_method="INDEX_PUT_SPREAD",
            hedge_cost_ratio=hedge_cost_ratio,
        )

    if grade == "A":
        return HedgeAction(
            required=False,
            method="NO_HEDGE",
            size_reduction_pct=0.0,
            alt_method="TAIL_PUT",
            hedge_cost_ratio=hedge_cost_ratio,
        )
    if grade == "B":
        return HedgeAction(
            required=True,
            method="SIZE_REDUCTION",
            size_reduction_pct=0.15,
            alt_method="INDEX_PUT_SPREAD",
            hedge_cost_ratio=hedge_cost_ratio,
        )
    return HedgeAction(
        required=True,
        method="SIZE_REDUCTION",
        size_reduction_pct=0.35,
        alt_method="INDEX_PUT_SPREAD",
        hedge_cost_ratio=hedge_cost_ratio,
    )


def _size_reduction_for_grade(grade: str) -> float:
    if grade == "A":
        return 0.1
    if grade == "B":
        return 0.25
    return 0.4
