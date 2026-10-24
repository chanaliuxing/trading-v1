from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProtectionThresholds:
    self_heal_threshold: int
    kill_switch_threshold: int


@dataclass(frozen=True)
class ProtectionAuditState:
    protection_active: bool
    protection_pending: bool
    miss_count: int
    recovered: bool
    stop_visible: bool


def protection_thresholds(*, self_heal_after: int, kill_switch_after: int) -> ProtectionThresholds:
    self_heal = max(1, int(self_heal_after))
    kill_switch = max(self_heal, int(kill_switch_after))
    return ProtectionThresholds(
        self_heal_threshold=self_heal,
        kill_switch_threshold=kill_switch,
    )


def protection_audit_state(
    *,
    prev_miss_count: int,
    has_live_stop: bool | None,
    protection_active: bool = False,
    protection_pending: bool = False,
) -> ProtectionAuditState:
    if has_live_stop is True:
        return ProtectionAuditState(
            protection_active=True,
            protection_pending=False,
            miss_count=0,
            recovered=int(prev_miss_count) > 0,
            stop_visible=True,
        )
    if has_live_stop is False:
        return ProtectionAuditState(
            protection_active=False,
            protection_pending=True,
            miss_count=max(0, int(prev_miss_count)) + 1,
            recovered=False,
            stop_visible=False,
        )
    return ProtectionAuditState(
        protection_active=bool(protection_active),
        protection_pending=bool(protection_pending),
        miss_count=0,
        recovered=False,
        stop_visible=False,
    )


def should_wait_for_self_heal(*, miss_count: int, self_heal_threshold: int) -> bool:
    return int(miss_count) < max(1, int(self_heal_threshold))


def should_kill_switch(*, miss_count: int, kill_switch_threshold: int) -> bool:
    return int(miss_count) >= max(1, int(kill_switch_threshold))


def stop_audit_startup_grace_until(*, loop_now_mono: float, startup_grace_sec: float) -> float:
    return float(loop_now_mono) + max(2.0, float(startup_grace_sec))
