from __future__ import annotations

from strategies.qqq_intraday_protection_logic import (
    protection_audit_state,
    protection_thresholds,
    should_kill_switch,
    should_wait_for_self_heal,
    stop_audit_startup_grace_until,
)


def test_protection_audit_state_recovers_visible_stop() -> None:
    state = protection_audit_state(prev_miss_count=2, has_live_stop=True)
    assert state.protection_active is True
    assert state.protection_pending is False
    assert state.miss_count == 0
    assert state.recovered is True
    assert state.stop_visible is True


def test_protection_audit_state_increments_missing_stop() -> None:
    state = protection_audit_state(prev_miss_count=2, has_live_stop=False)
    assert state.protection_active is False
    assert state.protection_pending is True
    assert state.miss_count == 3
    assert state.recovered is False
    assert state.stop_visible is False


def test_protection_audit_state_preserves_runtime_flags_when_broker_visibility_unknown() -> None:
    state = protection_audit_state(
        prev_miss_count=2,
        has_live_stop=None,
        protection_active=True,
        protection_pending=True,
    )
    assert state.protection_active is True
    assert state.protection_pending is True
    assert state.miss_count == 0


def test_protection_thresholds_and_decisions() -> None:
    thresholds = protection_thresholds(self_heal_after=3, kill_switch_after=2)
    assert thresholds.self_heal_threshold == 3
    assert thresholds.kill_switch_threshold == 3
    assert should_wait_for_self_heal(miss_count=2, self_heal_threshold=thresholds.self_heal_threshold) is True
    assert should_kill_switch(miss_count=3, kill_switch_threshold=thresholds.kill_switch_threshold) is True


def test_stop_audit_startup_grace_until_has_minimum_floor() -> None:
    assert stop_audit_startup_grace_until(loop_now_mono=10.0, startup_grace_sec=0.5) == 12.0
    assert stop_audit_startup_grace_until(loop_now_mono=10.0, startup_grace_sec=6.0) == 16.0
