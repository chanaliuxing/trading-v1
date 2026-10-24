from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Mapping

RUNTIME_DATETIME_EXTRA_KEYS: tuple[str, ...] = (
    "entry_time_et",
    "pending_decision_since_et",
    "pending_decision_last_probe_et",
    "entry_timeout_block_until_et",
    "last_entry_timeout_block_warn_et",
    "last_flat_orphan_sweep_et",
    "last_stop_exit_at",
)


def serialize_runtime_extras(extras: Mapping[str, Any] | None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in dict(extras or {}).items():
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    return payload


def restore_runtime_extras(
    extras: Mapping[str, Any] | None,
    *,
    coerce_datetime: Callable[[Any], datetime | None],
    datetime_keys: tuple[str, ...] = RUNTIME_DATETIME_EXTRA_KEYS,
) -> Dict[str, Any]:
    restored = dict(extras or {})
    for dt_key in datetime_keys:
        restored_dt = coerce_datetime(restored.get(dt_key))
        if restored_dt is not None:
            restored[dt_key] = restored_dt
    return restored


def runtime_state_snapshot_row(
    state: Any,
    *,
    serialize_extras: Callable[[Mapping[str, Any] | None], Dict[str, Any]] = serialize_runtime_extras,
) -> Dict[str, Any]:
    return {
        "is_traded": state.is_traded,
        "is_flattening": state.is_flattening,
        "active_side": state.active_side,
        "active_exec_symbol": state.active_exec_symbol,
        "pending_decision_id": state.pending_decision_id,
        "active_trigger_id": state.active_trigger_id,
        "expected_entry_qty": state.expected_entry_qty,
        "entry_order_ref": state.entry_order_ref,
        "entry_px": state.entry_px,
        "stop_px": state.stop_px,
        "entry_atr_used": state.entry_atr_used,
        "tp1_hit": state.tp1_hit,
        "trail_high_px": state.trail_high_px,
        "trail_floor_px": state.trail_floor_px,
        "breakeven_done": state.breakeven_done,
        "extras": serialize_extras(state.extras),
    }


def restore_runtime_state_row(
    *,
    state: Any,
    saved: Mapping[str, Any],
    optional_float: Callable[[Any], float | None],
) -> None:
    state.is_traded = bool(saved.get("is_traded", state.is_traded))
    state.is_flattening = bool(saved.get("is_flattening", state.is_flattening))
    state.active_side = saved.get("active_side") or state.active_side
    state.active_exec_symbol = saved.get("active_exec_symbol") or state.active_exec_symbol
    state.pending_decision_id = saved.get("pending_decision_id")
    state.active_trigger_id = saved.get("active_trigger_id")
    state.expected_entry_qty = int(saved.get("expected_entry_qty") or state.expected_entry_qty or 0)
    state.entry_order_ref = saved.get("entry_order_ref")
    state.entry_px = optional_float(saved.get("entry_px")) or state.entry_px
    state.stop_px = optional_float(saved.get("stop_px")) or state.stop_px
    state.entry_atr_used = optional_float(saved.get("entry_atr_used")) or state.entry_atr_used
    state.tp1_hit = bool(saved.get("tp1_hit", state.tp1_hit))
    state.trail_high_px = optional_float(saved.get("trail_high_px")) or state.trail_high_px
    state.trail_floor_px = optional_float(saved.get("trail_floor_px")) or state.trail_floor_px
    state.breakeven_done = bool(saved.get("breakeven_done", state.breakeven_done))


def build_runtime_snapshot_payload(
    *,
    strategy_id: str,
    generated_at_et: str,
    ownership_snapshot: Dict[str, Any],
    symbol_state: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "generated_at_et": generated_at_et,
        "ownership_ledger": ownership_snapshot,
        "replica_runtime": {
            symbol: runtime_state_snapshot_row(state) for symbol, state in symbol_state.items()
        },
    }


def remember_audit_context(
    audit_context_by_symbol: Dict[str, Dict[str, Any]],
    *,
    symbol: str,
    decision_id: str | None = None,
    trigger_id: str | None = None,
    trace_id: str | None = None,
    order_ref: str | None = None,
) -> Dict[str, Any]:
    symbol_u = str(symbol or "").strip().upper()
    current = dict(audit_context_by_symbol.get(symbol_u) or {})
    if decision_id:
        current["decision_id"] = str(decision_id)
    if trigger_id:
        current["trigger_id"] = str(trigger_id)
    if trace_id:
        current["trace_id"] = str(trace_id)
    if order_ref:
        current["order_ref"] = str(order_ref)
    audit_context_by_symbol[symbol_u] = current
    return current


def ensure_audit_context(
    audit_context_by_symbol: Dict[str, Dict[str, Any]],
    *,
    symbol: str,
    make_decision_id: Callable[[], str],
    trigger_id: str | None = None,
    order_ref: str | None = None,
) -> Dict[str, Any]:
    symbol_u = str(symbol or "").strip().upper()
    current = dict(audit_context_by_symbol.get(symbol_u) or {})
    trigger_val = str(trigger_id or current.get("trigger_id") or make_decision_id()).strip()
    trace_val = str(current.get("trace_id") or current.get("decision_id") or trigger_val).strip()
    return remember_audit_context(
        audit_context_by_symbol,
        symbol=symbol_u,
        decision_id=str(current.get("decision_id") or "").strip() or None,
        trigger_id=trigger_val or None,
        trace_id=trace_val or None,
        order_ref=str(order_ref or current.get("order_ref") or "").strip() or None,
    )


def resolve_audit_ids(
    audit_context: Mapping[str, Any] | None,
    *,
    decision_id: str | None = None,
    trigger_id: str | None = None,
    trace_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    current = dict(audit_context or {})
    decision_id_val = str(decision_id or current.get("decision_id") or "").strip() or None
    trigger_id_val = str(trigger_id or current.get("trigger_id") or "").strip() or None
    trace_id_val = str(trace_id or current.get("trace_id") or decision_id_val or trigger_id_val or "").strip() or None
    return decision_id_val, trigger_id_val, trace_id_val
