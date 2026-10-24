from __future__ import annotations

from datetime import time
from typing import Any

from .contracts import EntryContext, RuleDecision
from .session import in_window


def _parse_hms(raw: Any, default: time) -> time:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        hh, mm, ss = [int(part) for part in text.split(":")]
        return time(hh, mm, ss)
    except Exception:
        return default


def session_window_guard(ctx: EntryContext) -> RuleDecision:
    if in_window(ctx.now.time(), start_t=ctx.gate_start, end_t=ctx.gate_end):
        return RuleDecision(True, "session_window_open")
    return RuleDecision(False, "outside_session_window")


def amp_lock_guard(ctx: EntryContext) -> RuleDecision:
    if not bool(ctx.extras.get("entry_require_amp_lock", False)):
        return RuleDecision(True, "amp_lock_not_required")
    if bool(ctx.extras.get("amp_locked", True)):
        return RuleDecision(True, "amp_locked")
    return RuleDecision(False, "amp_not_locked")


def stable_entry_guard(ctx: EntryContext) -> RuleDecision:
    reason = str(ctx.extras.get("stable_entry_block_reason", "")).strip()
    if reason:
        return RuleDecision(False, reason)
    return RuleDecision(True, "stable_entry_guard_pass")


def restart_degrade_guard(ctx: EntryContext) -> RuleDecision:
    if bool(ctx.extras.get("restart_degrade_active", False)):
        return RuleDecision(False, "restart_degrade_active")
    return RuleDecision(True, "restart_degrade_clear")


def first_entry_seed_guard(ctx: EntryContext) -> RuleDecision:
    if not bool(ctx.extras.get("first_entry_require_seed_ready", False)):
        return RuleDecision(True, "first_entry_seed_not_required")
    if "seed_ready" in ctx.extras and not bool(ctx.extras.get("seed_ready")):
        return RuleDecision(False, "first_entry_seed_not_ready", {"seed_ready": False})
    try:
        seed_n = int(ctx.extras.get("seed_n", 0) or 0)
    except Exception:
        seed_n = 0
    try:
        seed_min = int(ctx.extras.get("first_entry_seed_min_samples", 600) or 600)
    except Exception:
        seed_min = 600
    if seed_n < max(1, seed_min):
        return RuleDecision(False, "first_entry_seed_not_ready", {"seed_n": seed_n, "seed_min_samples": seed_min})
    return RuleDecision(True, "first_entry_seed_ready")


def startup_anchor_guard(ctx: EntryContext) -> RuleDecision:
    if not bool(ctx.extras.get("startup_anchor_entry_block_enabled", False)):
        return RuleDecision(True, "startup_anchor_disabled")
    if "startup_anchor_entry_block_active" in ctx.extras:
        if not bool(ctx.extras.get("startup_anchor_entry_block_active")):
            return RuleDecision(True, "startup_anchor_clear")
    start_t = _parse_hms(ctx.extras.get("startup_anchor_entry_block_start", "09:30:00"), time(9, 30))
    end_t = _parse_hms(ctx.extras.get("startup_anchor_entry_block_end", "10:00:00"), time(10, 0))
    if start_t <= ctx.now.time() <= end_t:
        return RuleDecision(False, "startup_anchor_embargo")
    return RuleDecision(True, "startup_anchor_clear")


def open_orders_guard(ctx: EntryContext) -> RuleDecision:
    pending = int(ctx.extras.get("same_strategy_pending_orders", 0) or 0)
    if pending > 0:
        return RuleDecision(False, "open_orders_blocked", {"same_strategy_pending_orders": pending})
    return RuleDecision(True, "open_orders_clear")


def live_position_guard(ctx: EntryContext) -> RuleDecision:
    foreign_qty = float(ctx.extras.get("foreign_position_qty", 0.0) or 0.0)
    if abs(foreign_qty) > 1e-9:
        return RuleDecision(False, "live_position_blocked", {"foreign_position_qty": foreign_qty})
    return RuleDecision(True, "live_position_clear")


def pending_decision_sync_guard(ctx: EntryContext) -> RuleDecision:
    if bool(ctx.extras.get("pending_decision_sync", False)):
        return RuleDecision(False, "pending_decision_sync_blocked")
    return RuleDecision(True, "pending_decision_sync_clear")


def ownership_guard(ctx: EntryContext) -> RuleDecision:
    if bool(ctx.extras.get("ownership_conflict", False)):
        return RuleDecision(False, "ownership_conflict")
    return RuleDecision(True, "ownership_clear")


def qqq_freshness_guard(ctx: EntryContext) -> RuleDecision:
    primary_symbol = str(ctx.extras.get("primary_symbol") or ctx.symbol or "QQQ").strip().upper()
    primary_prefix = primary_symbol.lower()
    max_age = float(
        ctx.extras.get(
            f"entry_max_data_age_sec_{primary_prefix}",
            ctx.extras.get("entry_max_data_age_sec_primary", ctx.extras.get("entry_max_data_age_sec_qqq", ctx.extras.get("entry_max_data_age_sec", 3.0))),
        )
        or 3.0
    )
    if ctx.data_age_qqq_sec is None:
        return RuleDecision(True, "qqq_data_age_untracked")
    if float(ctx.data_age_qqq_sec) > max_age:
        return RuleDecision(False, "data_not_ready", {"data_age_qqq_sec": float(ctx.data_age_qqq_sec), "max_data_age_qqq_sec": max_age})
    return RuleDecision(True, "qqq_fresh")


def qqew_freshness_guard(ctx: EntryContext) -> RuleDecision:
    if ctx.qqew_decoupled:
        return RuleDecision(True, "qqew_decoupled")
    confirm_symbol = str(ctx.extras.get("confirm_symbol") or "").strip().upper()
    confirm_prefix = str(ctx.extras.get("confirm_feature_prefix") or confirm_symbol.lower() or "qqew").strip().lower()
    max_age = float(
        ctx.extras.get(
            f"entry_max_data_age_sec_{confirm_prefix}",
            ctx.extras.get("entry_max_data_age_sec_confirm", ctx.extras.get("entry_max_data_age_sec_qqew", ctx.extras.get("entry_max_data_age_sec", 3.0))),
        )
        or 3.0
    )
    if ctx.data_age_qqew_sec is None:
        return RuleDecision(True, "qqew_data_age_untracked")
    if float(ctx.data_age_qqew_sec) > max_age:
        return RuleDecision(False, "data_not_ready", {"data_age_qqew_sec": float(ctx.data_age_qqew_sec), "max_data_age_qqew_sec": max_age})
    return RuleDecision(True, "qqew_fresh")


def continuity_180s_guard(ctx: EntryContext) -> RuleDecision:
    if not bool(ctx.extras.get("entry_require_continuity_180s", False)):
        return RuleDecision(True, "continuity_not_required")
    if ctx.continuity_180s_ok:
        return RuleDecision(True, "continuity_180s_ok")
    return RuleDecision(False, "data_not_ready", {"continuity_180s_ok": False})


def data_integrity_fail_closed_guard(ctx: EntryContext) -> RuleDecision:
    if bool(ctx.extras.get("data_integrity_fail_closed", False)):
        return RuleDecision(False, "data_integrity_fail_closed")
    return RuleDecision(True, "data_integrity_clear")


def atr_ready_guard(ctx: EntryContext) -> RuleDecision:
    if ctx.atr_eff > 0 and ctx.atr_live > 0 and ctx.atr_seed > 0:
        return RuleDecision(True, "atr_ready")
    return RuleDecision(False, "data_not_ready", {"atr_live": ctx.atr_live, "atr_seed": ctx.atr_seed, "atr_eff": ctx.atr_eff})


def vwap_ready_guard(ctx: EntryContext) -> RuleDecision:
    if ctx.vwap_now > 0:
        return RuleDecision(True, "vwap_ready")
    return RuleDecision(False, "data_not_ready", {"vwap_now": ctx.vwap_now})


def volume_ready_guard(ctx: EntryContext) -> RuleDecision:
    require_ma10 = bool(ctx.extras.get("volume_guard_require_ma10", False))
    if not require_ma10 and (ctx.vol_ma10 is None or ctx.vol_ma10 <= 0):
        return RuleDecision(True, "volume_ma10_untracked")
    if ctx.vol_ma10 is None or ctx.vol_ma10 <= 0:
        return RuleDecision(False, "data_not_ready", {"vol_ma10": ctx.vol_ma10, "vol_t": ctx.vol_t})
    return RuleDecision(True, "volume_ready")
