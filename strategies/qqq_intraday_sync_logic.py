from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PendingDecisionSyncPlan:
    should_timeout_clear: bool
    should_probe: bool
    should_clear_flat_no_active_orders: bool
    next_probe_count: int


@dataclass(frozen=True)
class DualConflictState:
    crossed_inventory: bool
    live_symbol: str | None


def sync_quarantine_reason(extras: Mapping[str, Any] | None) -> str:
    return str((extras or {}).get("sync_quarantine_reason") or "").strip()


def is_sync_quarantined(extras: Mapping[str, Any] | None) -> bool:
    return bool(sync_quarantine_reason(extras))


def sync_reason_or_default(extras: Mapping[str, Any] | None, *, default: str) -> str:
    reason = sync_quarantine_reason(extras)
    return reason or str(default or "").strip()


def evaluate_dual_conflict(*, qty_long: float, qty_short: float, min_abs: float) -> DualConflictState:
    long_live = abs(float(qty_long or 0.0)) > float(min_abs)
    short_live = abs(float(qty_short or 0.0)) > float(min_abs)
    if long_live and short_live:
        return DualConflictState(crossed_inventory=True, live_symbol=None)
    if long_live:
        return DualConflictState(crossed_inventory=False, live_symbol="LONG")
    if short_live:
        return DualConflictState(crossed_inventory=False, live_symbol="SHORT")
    return DualConflictState(crossed_inventory=False, live_symbol=None)


def evaluate_pending_decision_sync(
    *,
    age_sec: float | None,
    broker_flat: bool,
    min_hold_sec: float,
    timeout_sec: float,
    probe_interval_elapsed: bool,
    has_active_orders: bool | None,
    qty_long: float,
    qty_short: float,
    min_abs: float,
    current_probe_count: int,
    probe_need: int,
    ctx_read_failed: bool,
) -> PendingDecisionSyncPlan:
    if age_sec is not None and age_sec >= float(timeout_sec) and broker_flat:
        return PendingDecisionSyncPlan(
            should_timeout_clear=True,
            should_probe=False,
            should_clear_flat_no_active_orders=False,
            next_probe_count=0,
        )
    if age_sec is not None and age_sec < float(min_hold_sec):
        return PendingDecisionSyncPlan(False, False, False, int(current_probe_count))
    if not probe_interval_elapsed:
        return PendingDecisionSyncPlan(False, False, False, int(current_probe_count))
    if has_active_orders:
        return PendingDecisionSyncPlan(False, True, False, 0)
    if ctx_read_failed:
        return PendingDecisionSyncPlan(False, True, False, 0)
    if abs(float(qty_long or 0.0)) > float(min_abs) or abs(float(qty_short or 0.0)) > float(min_abs):
        return PendingDecisionSyncPlan(False, True, False, 0)
    next_probe_count = int(max(0, current_probe_count)) + 1
    return PendingDecisionSyncPlan(
        should_timeout_clear=False,
        should_probe=True,
        should_clear_flat_no_active_orders=next_probe_count >= int(max(1, probe_need)),
        next_probe_count=next_probe_count,
    )
