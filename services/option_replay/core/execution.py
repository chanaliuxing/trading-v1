"""Execution and quote-state helpers for the option replay engine."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, List, Optional

import pandas as pd

from services.option_replay.core.accounting import apply_entry_fill, apply_exit_fill
from services.option_replay.core.audit import record_bracket_audit, record_fill, record_order, record_selection_audit
from services.option_replay.core.selector import (
    compute_contracts,
    describe_selection_snapshot,
    entry_fill_price,
    exit_fill_price,
    explain_contract_rejection,
    infer_iv_context,
    select_contract,
)
from services.option_replay.core.types import ContractQuote, PendingEntry, PendingSetup, PositionState
from services.option_replay.strategies.ma10_reject_0324 import aggressive_entry_allowed


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def chain_for(engine: Any, ts: datetime, right: str) -> List[ContractQuote]:
    key = (ts.replace(second=0, microsecond=0), right.upper())
    return list(engine.option_cache.get(key, []))


def _create_bracket_children(engine: Any, ts: datetime, position: PositionState) -> None:
    stop_order_id = engine._allocate_order_id("stop")
    tp_order_id = engine._allocate_order_id("tp")
    position.stop_order_id = stop_order_id
    position.tp_order_id = tp_order_id
    record_order(
        engine,
        ts,
        stop_order_id,
        "WORKING",
        parent_order_id=position.entry_order_id,
        order_role="STOP_CHILD",
        side=position.side,
        expiry=position.expiry,
        strike=position.strike,
        qty=position.remaining,
        trigger_underlying=position.structure_stop,
    )
    record_order(
        engine,
        ts,
        tp_order_id,
        "WORKING",
        parent_order_id=position.entry_order_id,
        order_role="TAKE_PROFIT_CHILD",
        side=position.side,
        expiry=position.expiry,
        strike=position.strike,
        qty=position.remaining,
        trigger_underlying=position.target_1r if position.contracts >= 2 else position.target_1p2r,
    )
    record_bracket_audit(
        engine,
        ts,
        "CHILDREN_CREATED",
        parent_order_id=position.entry_order_id,
        stop_order_id=stop_order_id,
        tp_order_id=tp_order_id,
        qty=position.remaining,
    )


def _sync_bracket_child_qty(engine: Any, ts: datetime, position: PositionState) -> None:
    if position.stop_order_id:
        record_order(
            engine,
            ts,
            position.stop_order_id,
            "REPLACED",
            parent_order_id=position.entry_order_id,
            order_role="STOP_CHILD",
            qty=position.remaining,
            trigger_underlying=position.structure_stop,
        )
    if position.tp_order_id:
        record_order(
            engine,
            ts,
            position.tp_order_id,
            "REPLACED",
            parent_order_id=position.entry_order_id,
            order_role="TAKE_PROFIT_CHILD",
            qty=position.remaining,
            trigger_underlying=position.target_1r if position.contracts >= 2 else position.target_1p2r,
        )
    record_bracket_audit(
        engine,
        ts,
        "CHILD_QTY_SYNC",
        parent_order_id=position.entry_order_id,
        stop_order_id=position.stop_order_id,
        tp_order_id=position.tp_order_id,
        qty=position.remaining,
    )


def _resolve_child_order(engine: Any, ts: datetime, position: PositionState, reason: str, qty: int) -> str:
    if qty < position.remaining and reason == "TP1_1R":
        triggered_order_id = position.tp_order_id or engine._allocate_order_id("tp")
        record_order(
            engine,
            ts,
            triggered_order_id,
            "PARTIAL_FILLED",
            parent_order_id=position.entry_order_id,
            qty=qty,
            remaining_qty=position.remaining - qty,
            exit_reason=reason,
        )
        record_bracket_audit(
            engine,
            ts,
            "CHILD_PARTIAL_TRIGGERED",
            parent_order_id=position.entry_order_id,
            fill_order_id=triggered_order_id,
            reason=reason,
            filled_qty=qty,
            remaining_qty=position.remaining - qty,
        )
        return triggered_order_id
    triggered_order_id = position.tp_order_id
    cancelled_order_id = position.stop_order_id
    if "STOP" in reason:
        triggered_order_id = position.stop_order_id
        cancelled_order_id = position.tp_order_id
    if "MA10_FAIL" in reason and position.tp_order_id:
        triggered_order_id = position.tp_order_id
        cancelled_order_id = position.stop_order_id
    if "EOD" in reason or "FINAL_BAR" in reason:
        triggered_order_id = engine._allocate_order_id("flatten")
        cancelled_order_id = position.tp_order_id or position.stop_order_id
        record_order(
            engine,
            ts,
            triggered_order_id,
            "FILLED",
            parent_order_id=position.entry_order_id,
            order_role="FLATTEN_CHILD",
            side=position.side,
            expiry=position.expiry,
            strike=position.strike,
            qty=qty,
            exit_reason=reason,
        )
        record_bracket_audit(
            engine,
            ts,
            "FLATTEN_TRIGGERED",
            parent_order_id=position.entry_order_id,
            fill_order_id=triggered_order_id,
            cancelled_order_id=cancelled_order_id,
            reason=reason,
        )
        if cancelled_order_id:
            record_order(
                engine,
                ts,
                cancelled_order_id,
                "CANCELLED",
                parent_order_id=position.entry_order_id,
                cancel_reason=f"oco_cancelled_by_{triggered_order_id}",
            )
        return triggered_order_id
    if triggered_order_id:
        record_order(
            engine,
            ts,
            triggered_order_id,
            "FILLED",
            parent_order_id=position.entry_order_id,
            qty=qty,
            exit_reason=reason,
        )
    if cancelled_order_id:
        record_order(
            engine,
            ts,
            cancelled_order_id,
            "CANCELLED",
            parent_order_id=position.entry_order_id,
            cancel_reason=f"oco_cancelled_by_{triggered_order_id}",
        )
    record_bracket_audit(
        engine,
        ts,
        "CHILD_TRIGGERED",
        parent_order_id=position.entry_order_id,
        fill_order_id=triggered_order_id,
        cancelled_order_id=cancelled_order_id,
        reason=reason,
        qty=qty,
    )
    return triggered_order_id or engine._allocate_order_id("exit")


def _entry_fill_qty(engine: Any, pending_entry: PendingEntry) -> int:
    remaining = max(0, pending_entry.contracts - pending_entry.filled_contracts)
    if remaining <= 0:
        return 0
    if not engine.cfg.allow_partial_fills or engine.cfg.max_fill_contracts_per_event <= 0:
        return remaining
    return min(remaining, int(engine.cfg.max_fill_contracts_per_event))


def _momentum_limit_bump(cfg: Any, attempt_count: int) -> float:
    profile = str(getattr(cfg, "momentum_chase_profile", "neutral") or "neutral").strip().lower()
    profiles = {
        "conservative": (0.01, 0.03),
        "neutral": (0.03, 0.08),
        "aggressive": (0.06, 0.15),
    }
    if profile in profiles:
        first, second = profiles[profile]
    else:
        first = float(getattr(cfg, "momentum_entry_first_try_limit_bump_pct", 0.03))
        second = float(getattr(cfg, "momentum_entry_second_try_limit_bump_pct", 0.08))
    return first if attempt_count <= 1 else second


def _pending_timeout_bars(engine: Any) -> int:
    base = int(getattr(engine.cfg, "pending_entry_timeout_bars", 2))
    if engine.pending_entry is None:
        return max(1, base)
    if str(getattr(engine.pending_entry, "signal_kind", "") or "").startswith("trend_continuation"):
        override = int(getattr(engine.cfg, "continuation_pending_entry_timeout_bars", 0) or 0)
        if override > 0:
            return max(1, override)
    return max(1, base)


def _continuation_reprice_limit(engine: Any, quote: ContractQuote) -> Optional[float]:
    if engine.pending_entry is None:
        return None
    signal_kind = str(getattr(engine.pending_entry, "signal_kind", "") or "")
    if not signal_kind.startswith("trend_continuation"):
        return None
    if bool(getattr(engine.cfg, "continuation_dynamic_reprice_to_ask", False)):
        return float(quote.ask)
    profile = str(getattr(engine.cfg, "continuation_reprice_profile", "") or "").strip().lower()
    if profile != "neutral":
        return None
    attempt = max(1, int(getattr(engine.pending_entry, "attempt_count", 1)))
    if attempt == 1:
        return max(float(quote.bid), float(quote.ask) - float(quote.spread) * 0.10)
    if attempt == 2:
        return max(float(quote.bid), float(quote.ask) - float(quote.spread) * 0.05)
    return float(quote.ask)


def _continuation_expansion_allowed(engine: Any, quote: ContractQuote) -> bool:
    if engine.pending_entry is None:
        return True
    signal_kind = str(getattr(engine.pending_entry, "signal_kind", "") or "")
    if not signal_kind.startswith("trend_continuation"):
        return True
    max_expansion = float(getattr(engine.cfg, "continuation_max_premium_expansion_pct", 0.0) or 0.0)
    relaxed_expansion = float(getattr(engine.cfg, "continuation_relaxed_premium_expansion_pct", 0.0) or 0.0)
    if relaxed_expansion > max_expansion:
        allow_relax = True
        if bool(getattr(engine.cfg, "continuation_relax_require_5m_trend_gate", False)):
            allow_relax = False
            trigger_idx = int(getattr(engine.pending_entry, "trigger_idx", -1))
            if 0 <= trigger_idx < len(engine.df):
                trigger_row = engine.df.iloc[trigger_idx]
                allow_relax = _passes_5m_trend_gate(
                    trigger_row,
                    engine.pending_entry.side,
                    allow_ma200_missing=bool(
                        getattr(engine.cfg, "continuation_relax_5m_gate_allow_ma200_missing", False)
                    ),
                )
        if allow_relax:
            min_vol_ratio = float(getattr(engine.cfg, "continuation_relax_min_vol_ratio", 0.0) or 0.0)
            if min_vol_ratio > 0:
                trigger_idx = int(getattr(engine.pending_entry, "trigger_idx", -1))
                if 0 <= trigger_idx < len(engine.df):
                    trigger_row = engine.df.iloc[trigger_idx]
                    vol = _safe_float(trigger_row.get("volume"))
                    vol_ma20 = max(1e-9, _safe_float(trigger_row.get("vol_ma20")))
                    if (vol / vol_ma20) >= min_vol_ratio:
                        max_expansion = relaxed_expansion
                # If trigger row is unavailable, keep the base cap.
            else:
                max_expansion = relaxed_expansion
    if max_expansion <= 0:
        return True
    base_price = float(getattr(engine.pending_entry, "limit_price", 0.0) or 0.0)
    if base_price <= 0:
        return True
    return ((float(quote.ask) / base_price) - 1.0) <= max_expansion + 1e-9


def _pending_entry_still_valid(engine: Any, row: pd.Series) -> tuple[bool, str]:
    if engine.pending_entry is None:
        return False, "pending_missing"
    side = engine.pending_entry.side
    close_px = _safe_float(row.get("close"))
    ma10 = _safe_float(row.get("ma10"))
    close_pos = _safe_float(row.get("close_pos"), 0.5)
    if side == "CALL":
        if ma10 > 0 and close_px <= ma10:
            return False, "ma10_side_lost"
        return True, "ok"
    # PUT side
    if ma10 > 0 and close_px >= ma10:
        return False, "ma10_side_lost"
    if close_pos > float(engine.cfg.put_close_pos_max_for_entry):
        return False, "put_reversal_close_pos"
    return True, "ok"


def chain_for_recent(engine: Any, ts: datetime, right: str) -> List[ContractQuote]:
    exact = chain_for(engine, ts, right)
    if exact:
        return exact
    for mins in range(1, int(engine.cfg.chain_lookup_back_min) + 1):
        prev = chain_for(engine, ts - timedelta(minutes=mins), right)
        if prev:
            return prev
    return []


def mark_quote(engine: Any, ts: datetime, expiry: str, strike: float, right: str) -> Optional[ContractQuote]:
    chain = chain_for_recent(engine, ts, right)
    exact = [q for q in chain if q.expiry == expiry and math.isclose(q.strike, strike)]
    if exact:
        quote = min(exact, key=lambda q: abs(q.strike - strike))
        engine.last_quote_by_contract[(expiry, strike, right)] = quote
        return quote
    last = engine.last_quote_by_contract.get((expiry, strike, right))
    if last is None:
        return None
    if (ts - last.ts) > timedelta(minutes=engine.cfg.quote_staleness_min):
        return None
    return last


def _resolve_conditional_delta_override(
    engine: Any,
    setup: PendingSetup,
    trigger_row: pd.Series,
) -> tuple[Optional[float], Optional[float], bool]:
    if not bool(getattr(engine.cfg, "low_delta_experiment_enabled", False)):
        return None, None, False
    raw_kinds = str(getattr(engine.cfg, "low_delta_signal_kinds_csv", "") or "")
    kinds = {k.strip() for k in raw_kinds.split(",") if k.strip()}
    if kinds and setup.signal_kind not in kinds:
        return None, None, False
    if bool(getattr(engine.cfg, "low_delta_require_trend_regime", True)):
        if not str(setup.regime or "").startswith("TREND_"):
            return None, None, False

    if bool(getattr(engine.cfg, "low_delta_require_5m_trend_gate", True)):
        allow_ma200_missing = bool(getattr(engine.cfg, "low_delta_5m_gate_allow_ma200_missing", False))
        if not _passes_5m_trend_gate(trigger_row, setup.side, allow_ma200_missing=allow_ma200_missing):
            return None, None, False

    min_vol_ratio = float(getattr(engine.cfg, "low_delta_min_vol_ratio", 1.0) or 0.0)
    if min_vol_ratio > 0:
        vol = _safe_float(trigger_row.get("volume"))
        vol_ma20 = max(1e-9, _safe_float(trigger_row.get("vol_ma20")))
        if (vol / vol_ma20) < min_vol_ratio:
            return None, None, False
    dmin = float(getattr(engine.cfg, "low_delta_delta_min", 0.30))
    dmax = float(getattr(engine.cfg, "low_delta_delta_max", 0.40))
    if dmin <= 0 or dmax <= 0 or dmin >= dmax:
        return None, None, False
    no_fallback = bool(getattr(engine.cfg, "low_delta_no_fallback", True))
    return dmin, dmax, no_fallback


def _branch_guard_kinds(engine: Any) -> set[str]:
    raw = str(getattr(engine.cfg, "branch_guard_signal_kinds_csv", "") or "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _prefilter_signal_kinds(engine: Any) -> set[str]:
    raw = str(getattr(engine.cfg, "prefilter_signal_kinds_csv", "") or "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _pre_entry_structural_filter(
    engine: Any,
    setup: PendingSetup,
    trigger_row: pd.Series,
) -> tuple[bool, str, dict[str, float]]:
    if not bool(getattr(engine.cfg, "prefilter_enabled", False)):
        return True, "", {}
    kinds = _prefilter_signal_kinds(engine)
    if kinds and setup.signal_kind not in kinds:
        return True, "", {}

    close_pos = _safe_float(trigger_row.get("close_pos"), 0.5)
    vol = _safe_float(trigger_row.get("volume"))
    vol_ma20 = max(1e-9, _safe_float(trigger_row.get("vol_ma20")))
    vol_ratio = vol / vol_ma20 if vol > 0 else 0.0
    if vol_ratio < float(getattr(engine.cfg, "prefilter_min_vol_ratio", 1.0) or 0.0):
        return False, "prefilter_low_vol_ratio", {"vol_ratio": float(vol_ratio)}

    if setup.side == "PUT":
        cp_min = float(getattr(engine.cfg, "prefilter_put_close_pos_min", 0.10) or 0.0)
        cp_max = float(getattr(engine.cfg, "prefilter_put_close_pos_max", 0.75) or 1.0)
        if close_pos < cp_min or close_pos > cp_max:
            return False, "prefilter_put_close_pos_extreme", {"close_pos": float(close_pos)}
    else:
        cp_min = float(getattr(engine.cfg, "prefilter_call_close_pos_min", 0.25) or 0.0)
        cp_max = float(getattr(engine.cfg, "prefilter_call_close_pos_max", 0.90) or 1.0)
        if close_pos < cp_min or close_pos > cp_max:
            return False, "prefilter_call_close_pos_extreme", {"close_pos": float(close_pos)}

    if bool(getattr(engine.cfg, "prefilter_require_5m_trend_align", True)):
        ok_5m = _passes_5m_trend_gate(
            trigger_row,
            setup.side,
            allow_ma200_missing=bool(getattr(engine.cfg, "prefilter_5m_allow_ma200_missing", True)),
        )
        if not ok_5m:
            return False, "prefilter_5m_trend_mismatch", {"close_pos": float(close_pos), "vol_ratio": float(vol_ratio)}
    return True, "", {"close_pos": float(close_pos), "vol_ratio": float(vol_ratio)}


def _passes_5m_trend_gate(trigger_row: pd.Series, side: str, *, allow_ma200_missing: bool = False) -> bool:
    ma10_5m = _safe_float(trigger_row.get("ma10_5m"))
    ma20_5m = _safe_float(trigger_row.get("ma20_5m"))
    ma200_5m = _safe_float(trigger_row.get("ma200_5m"))
    close_5m = _safe_float(trigger_row.get("close_5m"))
    slope_5m = _safe_float(trigger_row.get("ma10_5m_slope"))
    close_1m = _safe_float(trigger_row.get("close"))
    ma10_1m = _safe_float(trigger_row.get("ma10"))
    if min(ma10_5m, ma20_5m, close_5m, close_1m, ma10_1m) <= 0:
        return False

    if ma200_5m <= 0:
        if not allow_ma200_missing:
            return False
        if side == "PUT":
            return close_5m < ma10_5m < ma20_5m and slope_5m < 0 and close_1m < ma10_1m
        return close_5m > ma10_5m > ma20_5m and slope_5m > 0 and close_1m > ma10_1m

    if side == "PUT":
        return close_5m < ma10_5m < ma20_5m < ma200_5m and slope_5m < 0 and close_1m < ma10_1m
    return close_5m > ma10_5m > ma20_5m > ma200_5m and slope_5m > 0 and close_1m > ma10_1m


def _branch_risk_kinds(engine: Any) -> set[str]:
    raw = str(getattr(engine.cfg, "branch_risk_signal_kinds_csv", "") or "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _is_branch_risk_signal(engine: Any, signal_kind: str) -> bool:
    if not bool(getattr(engine.cfg, "branch_risk_control_enabled", False)):
        return False
    kinds = _branch_risk_kinds(engine)
    if not kinds:
        return False
    return signal_kind in kinds


def _branch_guard_check(engine: Any, ts: datetime, signal_kind: str) -> tuple[bool, str, dict[str, float | int]]:
    if not bool(getattr(engine.cfg, "branch_guard_enabled", False)):
        return True, "", {}
    kinds = _branch_guard_kinds(engine)
    if kinds and signal_kind not in kinds:
        return True, "", {}

    extra: dict[str, float | int] = {}
    max_dd = float(getattr(engine.cfg, "branch_guard_daily_drawdown_pct", 0.0) or 0.0)
    if max_dd > 0:
        day_start_nav = max(1e-9, float(getattr(engine, "day_start_nav", float(engine.cfg.starting_cash))))
        nav_now = float(engine._current_nav()) if hasattr(engine, "_current_nav") else float(engine.cash)
        dd_pct = max(0.0, (day_start_nav - nav_now) / day_start_nav)
        extra["day_start_nav"] = float(day_start_nav)
        extra["nav_now"] = float(nav_now)
        extra["day_drawdown_pct"] = float(dd_pct)
        if dd_pct >= max_dd:
            return False, "branch_guard_daily_dd", extra

    max_entries = int(getattr(engine.cfg, "branch_guard_max_entries_per_day", 0) or 0)
    if max_entries > 0:
        filled_count = int(getattr(engine, "day_branch_fill_counts", {}).get(signal_kind, 0))
        extra["branch_entries_today"] = filled_count
        extra["branch_entries_limit"] = max_entries
        if filled_count >= max_entries:
            return False, "branch_guard_max_entries_day", extra
    return True, "", extra


def _count_branch_fill_once(engine: Any, pending: PendingEntry) -> None:
    if not bool(getattr(engine.cfg, "branch_guard_enabled", False)):
        return
    kinds = _branch_guard_kinds(engine)
    if kinds and pending.signal_kind not in kinds:
        return
    counts = getattr(engine, "day_branch_fill_counts", None)
    if not isinstance(counts, dict):
        return
    counts[pending.signal_kind] = int(counts.get(pending.signal_kind, 0)) + 1


def _branch_contract_scale(engine: Any, signal_kind: str) -> float:
    if not _is_branch_risk_signal(engine, signal_kind):
        return 1.0
    scale = float(getattr(engine.cfg, "branch_contract_scale", 1.0) or 0.0)
    if scale <= 0:
        return 0.0
    return scale


def _branch_quality_gate(engine: Any, setup: PendingSetup, trigger_row: pd.Series) -> tuple[bool, str, dict[str, float | int]]:
    if not _is_branch_risk_signal(engine, setup.signal_kind):
        return True, "", {}
    if not bool(getattr(engine.cfg, "branch_quality_score_enabled", False)):
        return True, "", {}

    open_px = _safe_float(trigger_row.get("open"))
    high_px = _safe_float(trigger_row.get("high"))
    low_px = _safe_float(trigger_row.get("low"))
    close_px = _safe_float(trigger_row.get("close"))
    ma10 = _safe_float(trigger_row.get("ma10"))
    atr = max(1e-9, _safe_float(trigger_row.get("atr")))
    vol = _safe_float(trigger_row.get("volume"))
    vol_ma20 = max(1e-9, _safe_float(trigger_row.get("vol_ma20")))
    close_pos = _safe_float(trigger_row.get("close_pos"), 0.5)
    if min(open_px, high_px, low_px, close_px, ma10, atr, vol, vol_ma20) <= 0:
        return False, "branch_quality_missing_feature", {}

    rng = max(1e-9, high_px - low_px)
    body_frac = abs(close_px - open_px) / rng
    upper_wick = max(0.0, high_px - max(open_px, close_px))
    lower_wick = max(0.0, min(open_px, close_px) - low_px)
    adverse_wick_frac = (upper_wick / rng) if setup.side == "PUT" else (lower_wick / rng)
    vol_ratio = vol / vol_ma20
    dist_ma10_atr = abs(close_px - ma10) / atr

    min_body = float(getattr(engine.cfg, "branch_quality_min_body_frac", 0.0) or 0.0)
    max_adverse = float(getattr(engine.cfg, "branch_quality_max_adverse_wick_frac", 1.0) or 1.0)
    min_vol_ratio = float(getattr(engine.cfg, "branch_quality_min_vol_ratio", 0.0) or 0.0)
    max_dist = float(getattr(engine.cfg, "branch_quality_max_dist_ma10_atr", 0.0) or 0.0)
    put_close_cap = float(getattr(engine.cfg, "branch_quality_max_close_pos_put", 1.0) or 1.0)
    call_close_floor = float(getattr(engine.cfg, "branch_quality_min_close_pos_call", 0.0) or 0.0)
    score = 0
    if body_frac >= min_body:
        score += 1
    if adverse_wick_frac <= max_adverse:
        score += 1
    if vol_ratio >= min_vol_ratio:
        score += 1
    if max_dist <= 0 or dist_ma10_atr <= max_dist:
        score += 1
    if setup.side == "PUT":
        if close_pos <= put_close_cap:
            score += 1
    else:
        if close_pos >= call_close_floor:
            score += 1
    min_score = int(getattr(engine.cfg, "branch_quality_min_score", 0) or 0)
    boost = int(getattr(engine.cfg, "branch_quality_early_min_score_boost", 0) or 0)
    if boost > 0:
        ts = pd.Timestamp(trigger_row.get("ts")).to_pydatetime().replace(second=0, microsecond=0)
        try:
            start_t = datetime.strptime(str(getattr(engine.cfg, "branch_quality_early_window_start", "09:40")), "%H:%M").time()
            end_t = datetime.strptime(str(getattr(engine.cfg, "branch_quality_early_window_end", "10:00")), "%H:%M").time()
            in_early = start_t <= ts.time() <= end_t
        except Exception:
            in_early = False
        if in_early:
            min_score += boost
    extras: dict[str, float | int] = {
        "branch_quality_score": int(score),
        "branch_quality_min_score": int(min_score),
        "body_frac": float(body_frac),
        "adverse_wick_frac": float(adverse_wick_frac),
        "vol_ratio": float(vol_ratio),
        "dist_ma10_atr": float(dist_ma10_atr),
        "close_pos": float(close_pos),
    }
    if score < min_score:
        return False, "branch_quality_score_low", extras
    return True, "", extras


def _branch_risk_normalized_scale(engine: Any, setup: PendingSetup, r_per_share: float) -> float:
    if not _is_branch_risk_signal(engine, setup.signal_kind):
        return 1.0
    if not bool(getattr(engine.cfg, "branch_risk_norm_enabled", False)):
        return 1.0
    atr = max(1e-9, float(setup.atr))
    r_atr = max(1e-9, float(r_per_share) / atr)
    ref = max(1e-9, float(getattr(engine.cfg, "branch_risk_norm_ref_r_atr", 0.8) or 0.8))
    min_scale = max(0.0, float(getattr(engine.cfg, "branch_risk_norm_min_scale", 0.5) or 0.0))
    scale = min(1.0, ref / r_atr)
    return max(min_scale, scale)


def _branch_premium_expansion_allowed(engine: Any, pending: PendingEntry, quote: ContractQuote) -> bool:
    if not _is_branch_risk_signal(engine, pending.signal_kind):
        return True
    max_expansion = float(getattr(engine.cfg, "branch_max_premium_expansion_pct", 0.0) or 0.0)
    if max_expansion <= 0:
        return True
    base_price = float(getattr(pending, "limit_price", 0.0) or 0.0)
    if base_price <= 0:
        return True
    return ((float(quote.ask) / base_price) - 1.0) <= max_expansion + 1e-9


def create_pending_entry(engine: Any, idx: int, trigger_row: pd.Series, setup: PendingSetup) -> None:
    ts = pd.Timestamp(trigger_row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
    allowed, block_reason, block_extra = _branch_guard_check(engine, ts, setup.signal_kind)
    if not allowed:
        engine._record_skip(
            ts,
            block_reason,
            side=setup.side,
            grade=setup.grade,
            signal_kind=setup.signal_kind,
            **block_extra,
        )
        return

    quality_ok, quality_reason, quality_extra = _branch_quality_gate(engine, setup, trigger_row)
    if not quality_ok:
        engine._record_skip(
            ts,
            quality_reason,
            side=setup.side,
            grade=setup.grade,
            signal_kind=setup.signal_kind,
            **quality_extra,
        )
        return

    pre_ok, pre_reason, pre_extra = _pre_entry_structural_filter(engine, setup, trigger_row)
    if not pre_ok:
        engine._record_skip(
            ts,
            pre_reason,
            side=setup.side,
            grade=setup.grade,
            signal_kind=setup.signal_kind,
            **pre_extra,
        )
        return

    right = "C" if setup.side == "CALL" else "P"
    chain_now = chain_for_recent(engine, ts, right)
    chain_prev = chain_for_recent(engine, ts - timedelta(minutes=5), right)
    spot = _safe_float(trigger_row.get("close"))
    iv_now, iv_change = infer_iv_context(chain_now, chain_prev, spot)
    aggressive = aggressive_entry_allowed(setup, trigger_row, engine.cfg)
    delta_min_override, delta_max_override, no_delta_fallback = _resolve_conditional_delta_override(engine, setup, trigger_row)
    selection_snapshot = describe_selection_snapshot(
        chain_now,
        right=right,
        spot=spot,
        aggressive_same_day=aggressive,
        cfg=engine.cfg,
        delta_min_override=delta_min_override,
        delta_max_override=delta_max_override,
        no_delta_fallback=no_delta_fallback,
    )
    entry_contract = select_contract(
        chain_now,
        right=right,
        spot=spot,
        aggressive_same_day=aggressive,
        cfg=engine.cfg,
        delta_min_override=delta_min_override,
        delta_max_override=delta_max_override,
        no_delta_fallback=no_delta_fallback,
    )
    if entry_contract is None:
        reason = explain_contract_rejection(
            chain_now,
            right=right,
            aggressive_same_day=aggressive,
            cfg=engine.cfg,
            delta_min_override=delta_min_override,
            delta_max_override=delta_max_override,
            no_delta_fallback=no_delta_fallback,
        )
        record_selection_audit(
            engine,
            ts,
            side=setup.side,
            grade=setup.grade,
            regime=setup.regime,
            right=right,
            spot=spot,
            selected=False,
            rejection_reason=reason,
            candidate_count=len(selection_snapshot["top_candidates"]),
            top_candidates=selection_snapshot["top_candidates"],
        )
        engine._record_skip(ts, reason, side=setup.side, grade=setup.grade)
        return
    max_branch_spread = float(getattr(engine.cfg, "branch_max_spread_pct", 0.0) or 0.0)
    if _is_branch_risk_signal(engine, setup.signal_kind) and max_branch_spread > 0 and float(entry_contract.spread_pct) > max_branch_spread:
        engine._record_skip(
            ts,
            "branch_spread_too_wide",
            side=setup.side,
            grade=setup.grade,
            signal_kind=setup.signal_kind,
            spread_pct=float(entry_contract.spread_pct),
            spread=float(entry_contract.spread),
            mid=float(entry_contract.mid),
            max_branch_spread_pct=max_branch_spread,
        )
        return
    record_selection_audit(
        engine,
        ts,
        side=setup.side,
        grade=setup.grade,
        regime=setup.regime,
        right=right,
        spot=spot,
        selected=True,
        selected_expiry=entry_contract.expiry,
        selected_strike=float(entry_contract.strike),
        selected_dte=int(entry_contract.dte),
        selected_delta=float(entry_contract.delta),
        rejection_reason=None,
        candidate_count=len(selection_snapshot["top_candidates"]),
        top_candidates=selection_snapshot["top_candidates"],
    )
    if aggressive and iv_change is not None and iv_change < -engine.cfg.iv_fade_skip_abs:
        engine._record_skip(ts, "iv_fade_chase_skip", iv_change=iv_change, dte=entry_contract.dte)
        return

    iv_values = [q.iv for q in chain_now if q.iv > 0]
    iv_high = False
    if iv_now is not None and iv_values:
        ordered = sorted(iv_values)
        threshold_idx = min(len(ordered) - 1, int(len(ordered) * engine.cfg.iv_high_percentile))
        iv_high = iv_now >= ordered[threshold_idx]

    trigger_price = max(setup.high + engine.cfg.breakout_buffer, _safe_float(trigger_row.get("open")))
    if setup.side == "PUT":
        trigger_price = min(setup.low - engine.cfg.breakout_buffer, _safe_float(trigger_row.get("open")))
    structure_stop, target_1r, target_1p2r = engine.strategy.build_position_targets(engine.df, idx, trigger_row, setup, trigger_price)
    r_per_share = abs(trigger_price - structure_stop)
    if r_per_share <= 0:
        engine._record_skip(ts, "invalid_risk_distance")
        return

    contracts = compute_contracts(
        cash=engine.cash,
        grade=setup.grade,
        entry_quote=entry_contract,
        r_per_share=r_per_share,
        cfg=engine.cfg,
        iv_high=iv_high,
    )
    scale = _branch_contract_scale(engine, setup.signal_kind)
    if scale <= 0:
        contracts = 0
    elif abs(scale - 1.0) > 1e-9 and contracts > 0:
        contracts = max(1, int(math.floor(float(contracts) * scale)))
    norm_scale = _branch_risk_normalized_scale(engine, setup, r_per_share)
    if abs(norm_scale - 1.0) > 1e-9 and contracts > 0:
        contracts = max(1, int(math.floor(float(contracts) * norm_scale)))
    if contracts <= 0:
        engine._record_skip(ts, "size_zero", entry_mid=entry_contract.mid, grade=setup.grade)
        return

    limit_price = entry_fill_price(entry_contract, aggressive, engine.cfg)
    order_id = engine._allocate_order_id("entry")
    engine.pending_entry = PendingEntry(
        order_id=order_id,
        side=setup.side,
        regime=setup.regime,
        grade=setup.grade,
        created_ts=ts,
        trigger_ts=ts,
        trigger_idx=idx,
        expiry=entry_contract.expiry,
        strike=entry_contract.strike,
        trigger_underlying=trigger_price,
        limit_price=limit_price,
        entry_delta=entry_contract.delta,
        structure_stop=structure_stop,
        r_per_share=r_per_share,
        target_1r=target_1r,
        target_1p2r=target_1p2r,
        aggressive_contract=aggressive,
        iv_high=iv_high,
        contracts=contracts,
        signal_kind=setup.signal_kind,
    )
    record_order(
        engine,
        ts,
        order_id,
        "CREATED",
        order_type="ADAPTIVE_URGENT" if aggressive else "ADAPTIVE_NORMAL",
        side=setup.side,
        grade=setup.grade,
        expiry=entry_contract.expiry,
        strike=entry_contract.strike,
        qty=contracts,
        limit_price=limit_price,
    )
    engine._record_entry_audit(
        ts,
        "created",
        order_id=order_id,
        signal_kind=setup.signal_kind,
        side=setup.side,
        grade=setup.grade,
        regime=setup.regime,
        expiry=entry_contract.expiry,
        strike=entry_contract.strike,
        trigger_underlying=trigger_price,
        limit_price=limit_price,
        delta=entry_contract.delta,
        ask=entry_contract.ask,
        bid=entry_contract.bid,
        dte=entry_contract.dte,
    )


def try_fill_pending_entry(engine: Any, idx: int, row: pd.Series) -> None:
    if engine.pending_entry is None:
        return
    ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
    if idx <= engine.pending_entry.trigger_idx:
        return
    timeout_bars = _pending_timeout_bars(engine)
    if idx > engine.pending_entry.trigger_idx + timeout_bars:
        record_order(
            engine,
            ts,
            engine.pending_entry.order_id,
            "EXPIRED",
            side=engine.pending_entry.side,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            attempts=engine.pending_entry.attempt_count,
        )
        engine._record_skip(
            ts,
            "entry_timeout",
            side=engine.pending_entry.side,
            grade=engine.pending_entry.grade,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
        )
        engine._record_entry_audit(
            ts,
            "timeout",
            order_id=engine.pending_entry.order_id,
            side=engine.pending_entry.side,
            grade=engine.pending_entry.grade,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            attempts=engine.pending_entry.attempt_count,
        )
        engine.pending_entry = None
        return
    if bool(getattr(engine.cfg, "revalidate_pending_before_fill", True)):
        valid, reason = _pending_entry_still_valid(engine, row)
        if not valid:
            record_order(
                engine,
                ts,
                engine.pending_entry.order_id,
                "CANCELLED",
                side=engine.pending_entry.side,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                cancel_reason=f"signal_invalidated:{reason}",
            )
            engine._record_skip(
                ts,
                "entry_signal_invalidated",
                side=engine.pending_entry.side,
                grade=engine.pending_entry.grade,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                invalidate_reason=reason,
            )
            engine._record_entry_audit(
                ts,
                "invalidated",
                order_id=engine.pending_entry.order_id,
                signal_kind=engine.pending_entry.signal_kind,
                side=engine.pending_entry.side,
                grade=engine.pending_entry.grade,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                attempts=engine.pending_entry.attempt_count,
                invalidate_reason=reason,
            )
            engine.pending_entry = None
            return
    right = "C" if engine.pending_entry.side == "CALL" else "P"
    quote = engine._mark_quote(ts, engine.pending_entry.expiry, engine.pending_entry.strike, right)
    if quote is None:
        record_order(
            engine,
            ts,
            engine.pending_entry.order_id,
            "CANCELLED",
            side=engine.pending_entry.side,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            cancel_reason="quote_missing",
        )
        engine._record_skip(
            ts,
            "entry_quote_missing",
            side=engine.pending_entry.side,
            grade=engine.pending_entry.grade,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
        )
        engine._record_entry_audit(
            ts,
            "quote_missing",
            order_id=engine.pending_entry.order_id,
            side=engine.pending_entry.side,
            grade=engine.pending_entry.grade,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            attempts=engine.pending_entry.attempt_count,
        )
        engine.pending_entry = None
        return
    if not _branch_premium_expansion_allowed(engine, engine.pending_entry, quote):
        record_order(
            engine,
            ts,
            engine.pending_entry.order_id,
            "CANCELLED",
            side=engine.pending_entry.side,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            cancel_reason="branch_premium_expanded_too_far",
        )
        engine._record_skip(
            ts,
            "branch_premium_expanded_too_far",
            side=engine.pending_entry.side,
            grade=engine.pending_entry.grade,
            signal_kind=engine.pending_entry.signal_kind,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            limit_price=float(engine.pending_entry.limit_price),
            ask=float(quote.ask),
            max_expansion_pct=float(getattr(engine.cfg, "branch_max_premium_expansion_pct", 0.0) or 0.0),
        )
        engine.pending_entry = None
        return
    engine.pending_entry.attempt_count += 1
    if engine.pending_entry.attempt_count <= 1:
        chase_pct = engine.cfg.urgent_entry_chase_pct if engine.pending_entry.aggressive_contract else engine.cfg.adaptive_entry_chase_pct
    else:
        chase_pct = min(
            1.0,
            engine.cfg.urgent_entry_chase_pct if engine.pending_entry.aggressive_contract else engine.cfg.adaptive_entry_chase_pct + 0.20,
        )
    chase_limit = engine.pending_entry.limit_price + (quote.spread * chase_pct)
    if engine.pending_entry.signal_kind == "fresh_momentum":
        bump = _momentum_limit_bump(engine.cfg, engine.pending_entry.attempt_count)
        chase_limit = max(chase_limit, engine.pending_entry.limit_price * (1.0 + max(0.0, float(bump))))
    if str(getattr(engine.pending_entry, "signal_kind", "") or "").startswith("ma_cross_retest"):
        # Replay-only behavior: reprice to current ask for cross-retest continuation entries.
        chase_limit = max(chase_limit, float(quote.ask))
    if quote.ask > chase_limit + 1e-9:
        record_order(
            engine,
            ts,
            engine.pending_entry.order_id,
            "WORKING",
            side=engine.pending_entry.side,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            attempt=engine.pending_entry.attempt_count,
            quote_ask=float(quote.ask),
            chase_limit=float(chase_limit),
        )
        engine._record_entry_audit(
            ts,
            "missed",
            order_id=engine.pending_entry.order_id,
            signal_kind=engine.pending_entry.signal_kind,
            side=engine.pending_entry.side,
            grade=engine.pending_entry.grade,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            attempt=engine.pending_entry.attempt_count,
            limit_price=engine.pending_entry.limit_price,
            chase_limit=chase_limit,
            ask=float(quote.ask),
            bid=float(quote.bid),
        )
        continuation_limit = _continuation_reprice_limit(engine, quote)
        if continuation_limit is not None:
            chase_limit = max(chase_limit, continuation_limit)
        if not _continuation_expansion_allowed(engine, quote):
            if engine.pending_entry.attempt_count >= timeout_bars:
                record_order(
                    engine,
                    ts,
                    engine.pending_entry.order_id,
                    "CANCELLED",
                    side=engine.pending_entry.side,
                    expiry=engine.pending_entry.expiry,
                    strike=engine.pending_entry.strike,
                    cancel_reason="continuation_premium_expanded_too_far",
                )
                engine._record_skip(
                    ts,
                    "continuation_premium_expanded_too_far",
                    side=engine.pending_entry.side,
                    grade=engine.pending_entry.grade,
                    expiry=engine.pending_entry.expiry,
                    strike=engine.pending_entry.strike,
                    limit_price=engine.pending_entry.limit_price,
                    ask=float(quote.ask),
                    max_expansion_pct=float(getattr(engine.cfg, "continuation_max_premium_expansion_pct", 0.0) or 0.0),
                )
                engine.pending_entry = None
            return
        if quote.ask <= chase_limit + 1e-9:
            fill_px = min(chase_limit, quote.ask)
            fill_qty = _entry_fill_qty(engine, engine.pending_entry)
            if fill_qty <= 0:
                engine.pending_entry = None
                return
            should_count_branch_fill = engine.pending_entry.filled_contracts <= 0 and fill_qty > 0
            record_order(
                engine,
                ts,
                engine.pending_entry.order_id,
                "FILLED" if fill_qty >= (engine.pending_entry.contracts - engine.pending_entry.filled_contracts) else "PARTIAL_FILLED",
                side=engine.pending_entry.side,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                qty=fill_qty,
                remaining_qty=max(0, engine.pending_entry.contracts - engine.pending_entry.filled_contracts - fill_qty),
            )
            record_fill(
                engine,
                ts,
                engine.pending_entry.order_id,
                fill_px,
                fill_qty,
                side=engine.pending_entry.side,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                fill_reason="ENTRY_FILL",
            )
            apply_entry_fill(
                engine,
                ts,
                order_id=engine.pending_entry.order_id,
                qty=fill_qty,
                fill_px=fill_px,
                side=engine.pending_entry.side,
            )
            if engine.position is None:
                engine.position = PositionState(
                    entry_order_id=engine.pending_entry.order_id,
                    side=engine.pending_entry.side,
                    regime=engine.pending_entry.regime,
                    grade=engine.pending_entry.grade,
                    entry_ts=ts,
                    entry_idx=idx,
                    expiry=engine.pending_entry.expiry,
                    strike=engine.pending_entry.strike,
                    entry_underlying=engine.pending_entry.trigger_underlying,
                    entry_option=fill_px,
                    entry_delta=engine.pending_entry.entry_delta,
                    contracts=fill_qty,
                    remaining=fill_qty,
                    structure_stop=engine.pending_entry.structure_stop,
                    r_per_share=engine.pending_entry.r_per_share,
                    target_1r=engine.pending_entry.target_1r,
                    target_1p2r=engine.pending_entry.target_1p2r,
                    aggressive_contract=engine.pending_entry.aggressive_contract,
                    iv_high=engine.pending_entry.iv_high,
                    signal_kind=engine.pending_entry.signal_kind,
                    last_quote=quote,
                )
                _create_bracket_children(engine, ts, engine.position)
            else:
                prev_contracts = engine.position.contracts
                new_contracts = prev_contracts + fill_qty
                engine.position.entry_option = ((engine.position.entry_option * prev_contracts) + (fill_px * fill_qty)) / max(1, new_contracts)
                engine.position.contracts = new_contracts
                engine.position.remaining += fill_qty
                engine.position.last_quote = quote
                _sync_bracket_child_qty(engine, ts, engine.position)
            if should_count_branch_fill:
                _count_branch_fill_once(engine, engine.pending_entry)
            engine.pending_entry.filled_contracts += fill_qty
            engine._record_entry_audit(
                ts,
                "filled" if engine.pending_entry.filled_contracts >= engine.pending_entry.contracts else "partial_filled",
                order_id=engine.pending_entry.order_id,
                signal_kind=engine.pending_entry.signal_kind,
                side=engine.pending_entry.side,
                grade=engine.pending_entry.grade,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                attempt=engine.pending_entry.attempt_count,
                fill_qty=fill_qty,
                remaining_qty=max(0, engine.pending_entry.contracts - engine.pending_entry.filled_contracts),
                fill_price=fill_px,
                ask=float(quote.ask),
                bid=float(quote.bid),
                chase_limit=chase_limit,
            )
            if engine.pending_entry.filled_contracts >= engine.pending_entry.contracts or not engine.cfg.allow_partial_fills:
                engine.pending_entry = None
            return
        if engine.pending_entry.attempt_count >= timeout_bars:
            record_order(
                engine,
                ts,
                engine.pending_entry.order_id,
                "CANCELLED",
                side=engine.pending_entry.side,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                cancel_reason="entry_limit_missed",
            )
            engine._record_skip(
                ts,
                "entry_limit_missed",
                side=engine.pending_entry.side,
                grade=engine.pending_entry.grade,
                expiry=engine.pending_entry.expiry,
                strike=engine.pending_entry.strike,
                limit_price=engine.pending_entry.limit_price,
                chase_limit=chase_limit,
                ask=float(quote.ask),
            )
            engine.pending_entry = None
        return

    fill_px = min(chase_limit, quote.ask)
    fill_qty = _entry_fill_qty(engine, engine.pending_entry)
    if fill_qty <= 0:
        engine.pending_entry = None
        return
    should_count_branch_fill = engine.pending_entry.filled_contracts <= 0 and fill_qty > 0
    record_order(
        engine,
        ts,
        engine.pending_entry.order_id,
        "FILLED" if fill_qty >= (engine.pending_entry.contracts - engine.pending_entry.filled_contracts) else "PARTIAL_FILLED",
        side=engine.pending_entry.side,
        expiry=engine.pending_entry.expiry,
        strike=engine.pending_entry.strike,
        qty=fill_qty,
        remaining_qty=max(0, engine.pending_entry.contracts - engine.pending_entry.filled_contracts - fill_qty),
    )
    record_fill(
        engine,
        ts,
        engine.pending_entry.order_id,
        fill_px,
        fill_qty,
        side=engine.pending_entry.side,
        expiry=engine.pending_entry.expiry,
        strike=engine.pending_entry.strike,
        fill_reason="ENTRY_FILL",
    )
    apply_entry_fill(
        engine,
        ts,
        order_id=engine.pending_entry.order_id,
        qty=fill_qty,
        fill_px=fill_px,
        side=engine.pending_entry.side,
    )
    if engine.position is None:
        engine.position = PositionState(
            entry_order_id=engine.pending_entry.order_id,
            side=engine.pending_entry.side,
            regime=engine.pending_entry.regime,
            grade=engine.pending_entry.grade,
            entry_ts=ts,
            entry_idx=idx,
            expiry=engine.pending_entry.expiry,
            strike=engine.pending_entry.strike,
            entry_underlying=engine.pending_entry.trigger_underlying,
            entry_option=fill_px,
            entry_delta=engine.pending_entry.entry_delta,
            contracts=fill_qty,
            remaining=fill_qty,
            structure_stop=engine.pending_entry.structure_stop,
            r_per_share=engine.pending_entry.r_per_share,
            target_1r=engine.pending_entry.target_1r,
            target_1p2r=engine.pending_entry.target_1p2r,
            aggressive_contract=engine.pending_entry.aggressive_contract,
            iv_high=engine.pending_entry.iv_high,
            signal_kind=engine.pending_entry.signal_kind,
            last_quote=quote,
        )
        _create_bracket_children(engine, ts, engine.position)
    else:
        prev_contracts = engine.position.contracts
        new_contracts = prev_contracts + fill_qty
        engine.position.entry_option = ((engine.position.entry_option * prev_contracts) + (fill_px * fill_qty)) / max(1, new_contracts)
        engine.position.contracts = new_contracts
        engine.position.remaining += fill_qty
        engine.position.last_quote = quote
        _sync_bracket_child_qty(engine, ts, engine.position)
    if should_count_branch_fill:
        _count_branch_fill_once(engine, engine.pending_entry)
    engine.pending_entry.filled_contracts += fill_qty
    engine._record_entry_audit(
        ts,
        "filled" if engine.pending_entry.filled_contracts >= engine.pending_entry.contracts else "partial_filled",
        order_id=engine.pending_entry.order_id,
        signal_kind=engine.pending_entry.signal_kind,
        side=engine.pending_entry.side,
        grade=engine.pending_entry.grade,
        expiry=engine.pending_entry.expiry,
        strike=engine.pending_entry.strike,
        attempt=engine.pending_entry.attempt_count,
        fill_qty=fill_qty,
        remaining_qty=max(0, engine.pending_entry.contracts - engine.pending_entry.filled_contracts),
        fill_price=fill_px,
        ask=float(quote.ask),
        bid=float(quote.bid),
        chase_limit=chase_limit,
    )
    if engine.pending_entry.filled_contracts >= engine.pending_entry.contracts or not engine.cfg.allow_partial_fills:
        engine.pending_entry = None


def fallback_exit_quote(engine: Any, ts: datetime, side: str) -> Optional[ContractQuote]:
    if engine.position is None or engine.position.last_quote is None:
        return None
    last = engine.position.last_quote
    penalized_bid = max(0.01, last.bid * 0.90 if last.bid > 0 else last.mid * 0.75)
    return ContractQuote(
        ts=ts,
        expiry=last.expiry,
        right=last.right,
        strike=last.strike,
        dte=max(0, last.dte),
        bid=penalized_bid,
        ask=max(penalized_bid, last.ask),
        mid=max(penalized_bid, min(last.mid if last.mid > 0 else penalized_bid, last.ask if last.ask > 0 else penalized_bid)),
        iv=last.iv,
        delta=last.delta,
        gamma=last.gamma,
        theta=last.theta,
        vega=last.vega,
        open_interest=last.open_interest,
    )


def close_position(engine: Any, ts: datetime, quote: ContractQuote, underlying_px: float, qty: int, reason: str) -> None:
    if engine.position is None or qty <= 0:
        return
    qty = min(qty, engine.position.remaining)
    fill_px = exit_fill_price(quote, reason, engine.cfg)
    exit_order_id = _resolve_child_order(engine, ts, engine.position, reason, qty)
    record_fill(
        engine,
        ts,
        exit_order_id,
        fill_px,
        qty,
        side=engine.position.side,
        expiry=engine.position.expiry,
        strike=engine.position.strike,
        fill_reason=reason,
    )
    apply_exit_fill(
        engine,
        ts,
        order_id=exit_order_id,
        qty=qty,
        fill_px=fill_px,
        side=engine.position.side,
    )
    pnl = (fill_px - engine.position.entry_option) * qty * engine.cfg.contract_multiplier - (
        qty * engine.cfg.commission_per_contract * 2.0
    )
    engine.trades.append(
        {
            "entry_ts": engine.position.entry_ts.isoformat(),
            "exit_ts": ts.isoformat(),
            "side": engine.position.side,
            "grade": engine.position.grade,
            "regime": engine.position.regime,
            "expiry": engine.position.expiry,
            "strike": engine.position.strike,
            "qty": qty,
            "entry_underlying": engine.position.entry_underlying,
            "exit_underlying": underlying_px,
            "entry_option": engine.position.entry_option,
            "exit_option": fill_px,
            "entry_delta": engine.position.entry_delta,
            "signal_kind": engine.position.signal_kind if hasattr(engine.position, "signal_kind") else None,
            "structure_stop": engine.position.structure_stop,
            "target_1r": engine.position.target_1r,
            "target_1p2r": engine.position.target_1p2r,
            "entry_order_id": engine.position.entry_order_id,
            "exit_order_id": exit_order_id,
            "reason": reason,
            "pnl": pnl,
            "aggressive_contract": engine.position.aggressive_contract,
            "iv_high_halved": engine.position.iv_high,
            "hold_bars": max(0, engine.df.index[engine.df["ts"] == pd.Timestamp(ts)][0] - engine.position.entry_idx)
            if (engine.df["ts"] == pd.Timestamp(ts)).any()
            else None,
        }
    )
    engine.position.remaining -= qty
    engine.position.exit_reasons.append(reason)
    if engine.position.remaining > 0:
        _sync_bracket_child_qty(engine, ts, engine.position)
    if engine.position.remaining <= 0:
        engine.position = None


__all__ = [
    "chain_for",
    "chain_for_recent",
    "close_position",
    "create_pending_entry",
    "fallback_exit_quote",
    "mark_quote",
    "try_fill_pending_entry",
]
