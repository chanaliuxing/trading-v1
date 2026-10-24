from __future__ import annotations

from strategies.qqq_intraday_sync_logic import (
    evaluate_dual_conflict,
    evaluate_pending_decision_sync,
    is_sync_quarantined,
    sync_quarantine_reason,
    sync_reason_or_default,
)


def test_evaluate_dual_conflict_detects_crossed_inventory() -> None:
    result = evaluate_dual_conflict(qty_long=3.0, qty_short=2.0, min_abs=1e-9)
    assert result.crossed_inventory is True
    assert result.live_symbol is None


def test_evaluate_dual_conflict_returns_single_live_side() -> None:
    result = evaluate_dual_conflict(qty_long=3.0, qty_short=0.0, min_abs=1e-9)
    assert result.crossed_inventory is False
    assert result.live_symbol == "LONG"


def test_evaluate_pending_decision_sync_triggers_timeout_clear() -> None:
    plan = evaluate_pending_decision_sync(
        age_sec=61.0,
        broker_flat=True,
        min_hold_sec=8.0,
        timeout_sec=60.0,
        probe_interval_elapsed=True,
        has_active_orders=False,
        qty_long=0.0,
        qty_short=0.0,
        min_abs=1e-9,
        current_probe_count=0,
        probe_need=2,
        ctx_read_failed=False,
    )
    assert plan.should_timeout_clear is True


def test_evaluate_pending_decision_sync_clears_after_probe_need() -> None:
    plan = evaluate_pending_decision_sync(
        age_sec=12.0,
        broker_flat=True,
        min_hold_sec=8.0,
        timeout_sec=60.0,
        probe_interval_elapsed=True,
        has_active_orders=False,
        qty_long=0.0,
        qty_short=0.0,
        min_abs=1e-9,
        current_probe_count=1,
        probe_need=2,
        ctx_read_failed=False,
    )
    assert plan.should_probe is True
    assert plan.should_clear_flat_no_active_orders is True
    assert plan.next_probe_count == 2


def test_sync_quarantine_helpers() -> None:
    extras = {"sync_quarantine_reason": "crossed_inventory_detected"}
    assert sync_quarantine_reason(extras) == "crossed_inventory_detected"
    assert is_sync_quarantined(extras) is True
    assert sync_reason_or_default(extras, default="sync_from_real_position") == "crossed_inventory_detected"
    assert sync_reason_or_default({}, default="sync_from_real_position") == "sync_from_real_position"
