from __future__ import annotations

from typing import Any, Dict, Iterable

SIGNAL_BLOCK_PASSTHROUGH_REASONS: frozenset[str] = frozenset(
    {
        "atr_rank_not_ready",
        "atr_rank_skip",
        "pre_0945_no_early_override",
        "delayed_confirm_not_ready",
        "delayed_confirm_not_met",
        "early_lock_openclose_mismatch",
        "required_data_missing",
        "atr_eff_not_ready",
        "direction_mismatch",
        "missing_exec_price_for_sizing",
        "v3m_not_ready",
        "sign_mismatch",
        "fast_drift_not_ready",
        "fast_cooldown_active",
        "long_rollback_veto",
        "short_rollback_veto",
        "long_rebound_fail_veto",
        "short_rebound_fail_veto",
        "long_drift_second_confirm_missing",
        "long_drift_second_confirm_failed",
        "v47_long_chase_guard",
        "v47_short_chase_guard",
        "ab_opening_direction_not_ready",
        "ab_opening_direction_hard_gate",
        "ab_opening_sign_mismatch",
        "ab_opening_velocity_too_low",
        "ab_opening_momentum_decay",
        "ab_opening_volume_too_low",
        "ab_opening_overextended",
        "ab_a_time_cutoff",
        "ab_a_wait_pullback",
        "ab_a_mark_overextended",
        "ab_a_wait_mark",
        "ab_a_armed_expired",
        "ab_a_entry_overextended",
        "ab_a_runup_overextended",
        "ab_a_sign_mismatch",
        "ab_a_velocity_too_low",
        "ab_a_momentum_decay",
        "ab_a_volume_too_low",
        "elite_overextended",
        "elite_sign_mismatch",
        "elite_decay_window_insufficient",
        "elite_momentum_decay",
        "elite_volume_too_low",
        "elite_high_vol_sample_missing",
        "elite_high_vol_sign_not_confirmed",
        "high_vol_no_volume",
        "high_vol_protection",
        "day_shape_long_exhaustion",
        "day_shape_short_exhaustion",
    }
)

TRADER_REASON_MAPPING: Dict[str, str] = {
    "outside_ab_window": "OUTSIDE_AB_WINDOW",
    "after_no_new_entry": "AFTER_NO_NEW_ENTRY",
    "market_data_unavailable": "SIGNAL_GATE_FAIL_MARKET_DATA_UNAVAILABLE",
    "decision_pipeline_unavailable": "SIGNAL_GATE_FAIL_DECISION_PIPELINE_UNAVAILABLE",
    "required_data_missing": "SIGNAL_GATE_FAIL_REQUIRED_DATA_MISSING",
    "data_not_ready": "SIGNAL_GATE_FAIL_DATA_NOT_READY",
    "direction_mismatch": "SIGNAL_GATE_FAIL_DIRECTION_MISMATCH",
    "first_entry_seed_not_ready": "SIGNAL_GATE_FAIL_FIRST_ENTRY_SEED_NOT_READY",
    "restart_degrade_active": "SIGNAL_GATE_FAIL_RESTART_DEGRADE_ACTIVE",
    "startup_anchor_embargo": "SIGNAL_GATE_FAIL_STARTUP_ANCHOR_EMBARGO",
    "data_integrity_fail_closed": "SIGNAL_GATE_FAIL_DATA_INTEGRITY_FAIL_CLOSED",
    "extreme_blocked": "SIGNAL_GATE_FAIL_EXTREME_BLOCKED",
    "ownership_conflict": "OWNERSHIP_CONFLICT",
    "high_vol_no_volume": "SIGNAL_GATE_FAIL_HIGH_VOL_NO_VOLUME",
    "high_vol_protection": "SIGNAL_GATE_FAIL_HIGH_VOL_PROTECTION",
    "day_shape_long_exhaustion": "SIGNAL_GATE_FAIL_DAY_SHAPE_LONG_EXHAUSTION",
    "day_shape_short_exhaustion": "SIGNAL_GATE_FAIL_DAY_SHAPE_SHORT_EXHAUSTION",
    "signal_inactive_or_invalid": "SIGNAL_INACTIVE_OR_INVALID",
}


def signal_block_reason(raw_reason: Any, *, passthrough: Iterable[str] = SIGNAL_BLOCK_PASSTHROUGH_REASONS) -> str:
    raw = str(raw_reason or "").strip()
    if raw in set(passthrough):
        return raw
    return "signal_inactive_or_invalid"


def trader_reason_code(reason: Any, *, mapping: Dict[str, str] = TRADER_REASON_MAPPING) -> str:
    raw = str(reason or "").strip()
    if not raw:
        return "UNKNOWN"
    return mapping.get(raw.lower(), raw.upper())


def entry_block_payload(
    *,
    symbol: str,
    exec_symbol: str | None,
    reason: Any,
    trader_reason_code_value: str,
    gate_details: Dict[str, Any] | None,
) -> Dict[str, Any]:
    return {
        "signal_symbol": str(symbol or "").strip().upper() or None,
        "exec_symbol": str(exec_symbol or "").strip().upper() or None,
        "gate_reason": str(reason or "").strip() or "unknown",
        "trader_reason_code": str(trader_reason_code_value or "").strip().upper() or "UNKNOWN",
        "gate_details": dict(gate_details or {}),
    }
