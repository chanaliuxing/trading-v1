from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionGateDecision:
    allowed: bool
    reason: str


def in_session_window(
    *,
    now_et: _dt.datetime,
    start_hms: tuple[int, int, int] | None,
    end_hms: tuple[int, int, int] | None,
) -> bool:
    if start_hms is None or end_hms is None:
        return True
    now_t = now_et.timetz().replace(tzinfo=None)
    start_t = _dt.time(*start_hms)
    end_t = _dt.time(*end_hms)
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t


def session_window_gate(
    *,
    now_et: _dt.datetime,
    start_hms: tuple[int, int, int] | None,
    end_hms: tuple[int, int, int] | None,
    enforce_entry_window: bool,
) -> SessionGateDecision:
    if not enforce_entry_window:
        return SessionGateDecision(True, "session_window_not_enforced")
    if in_session_window(now_et=now_et, start_hms=start_hms, end_hms=end_hms):
        return SessionGateDecision(True, "session_window_open")
    return SessionGateDecision(False, "outside_session_window")


def previous_session_gate(
    *,
    require_prev_session_flat: bool,
    runtime_flat: bool,
    broker_flat: bool,
    open_orders_clear: bool,
    runtime_check_ok: bool,
    broker_check_ok: bool,
    open_orders_check_ok: bool,
) -> SessionGateDecision:
    if not require_prev_session_flat:
        return SessionGateDecision(True, "previous_session_flat_not_required")
    if not runtime_check_ok:
        return SessionGateDecision(False, "previous_session_runtime_check_unavailable")
    if not broker_check_ok:
        return SessionGateDecision(False, "previous_session_broker_check_unavailable")
    if not open_orders_check_ok:
        return SessionGateDecision(False, "previous_session_open_orders_check_unavailable")
    if not runtime_flat:
        return SessionGateDecision(False, "previous_session_runtime_not_flat")
    if not broker_flat:
        return SessionGateDecision(False, "previous_session_broker_not_flat")
    if not open_orders_clear:
        return SessionGateDecision(False, "previous_session_open_orders_not_clear")
    return SessionGateDecision(True, "previous_session_flat_confirmed")
