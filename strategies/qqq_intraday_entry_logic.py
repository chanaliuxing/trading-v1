from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeAddPlan:
    qty_total: int
    probe_qty: int
    add_qty: int


@dataclass(frozen=True)
class DayGrossNotionalCheck:
    allowed: bool
    cap: float
    existing_gross_notional: float
    planned_notional: float


def build_probe_add_plan(*, qty_total: int, probe_ratio: float = 0.70) -> ProbeAddPlan:
    total = int(max(0, qty_total))
    if total <= 0:
        return ProbeAddPlan(qty_total=0, probe_qty=0, add_qty=0)
    ratio = min(1.0, max(0.0, float(probe_ratio)))
    probe_qty = int(math.floor(float(total) * ratio))
    if probe_qty <= 0:
        probe_qty = total
    add_qty = max(0, int(total - probe_qty))
    return ProbeAddPlan(
        qty_total=total,
        probe_qty=int(probe_qty),
        add_qty=int(add_qty),
    )


def check_day_gross_notional_cap(
    *,
    cap: float,
    existing_long_qty: float,
    existing_short_qty: float,
    long_price: float,
    short_price: float,
    probe_qty: int,
    entry_price: float,
) -> DayGrossNotionalCheck:
    cap_f = float(max(0.0, cap))
    existing = (abs(float(existing_long_qty)) * float(max(0.0, long_price))) + (
        abs(float(existing_short_qty)) * float(max(0.0, short_price))
    )
    planned = float(max(0, int(probe_qty))) * float(max(0.0, entry_price))
    allowed = (existing + planned) <= (cap_f + 1e-6) if cap_f > 0 else True
    return DayGrossNotionalCheck(
        allowed=bool(allowed),
        cap=float(cap_f),
        existing_gross_notional=float(existing),
        planned_notional=float(planned),
    )
