from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from strategies.qqq_intraday_runtime_shell_logic import (
    build_runtime_snapshot_payload,
    ensure_audit_context,
    remember_audit_context,
    resolve_audit_ids,
    restore_runtime_extras,
    restore_runtime_state_row,
    serialize_runtime_extras,
)


def test_serialize_and_restore_runtime_extras_datetime() -> None:
    now = datetime.fromisoformat("2026-03-25T09:45:00-04:00")
    payload = serialize_runtime_extras({"entry_time_et": now, "x": 1})
    assert payload["entry_time_et"] == now.isoformat()

    restored = restore_runtime_extras(
        {"entry_time_et": now.isoformat(), "x": 1},
        coerce_datetime=lambda v: datetime.fromisoformat(str(v)) if v else None,
    )
    assert isinstance(restored["entry_time_et"], datetime)
    assert restored["x"] == 1


def test_build_runtime_snapshot_payload_rows() -> None:
    state = SimpleNamespace(
        is_traded=True,
        is_flattening=False,
        active_side="LONG",
        active_exec_symbol="TQQQ",
        pending_decision_id="d1",
        active_trigger_id="t1",
        expected_entry_qty=8,
        entry_order_ref="oref",
        entry_px=50.0,
        stop_px=48.5,
        entry_atr_used=0.7,
        tp1_hit=False,
        trail_high_px=None,
        trail_floor_px=None,
        breakeven_done=False,
        extras={"note": "ok"},
    )
    payload = build_runtime_snapshot_payload(
        strategy_id="index_super_win_replica_001",
        generated_at_et="2026-03-25T09:46:00-04:00",
        ownership_snapshot={"lots": []},
        symbol_state={"QQQ": state},
    )
    assert payload["strategy_id"] == "index_super_win_replica_001"
    assert payload["replica_runtime"]["QQQ"]["active_exec_symbol"] == "TQQQ"
    assert payload["replica_runtime"]["QQQ"]["extras"]["note"] == "ok"


def test_restore_runtime_state_row_fields() -> None:
    state = SimpleNamespace(
        is_traded=False,
        is_flattening=False,
        active_side=None,
        active_exec_symbol=None,
        pending_decision_id=None,
        active_trigger_id=None,
        expected_entry_qty=0,
        entry_order_ref=None,
        entry_px=None,
        stop_px=None,
        entry_atr_used=None,
        tp1_hit=False,
        trail_high_px=None,
        trail_floor_px=None,
        breakeven_done=False,
    )
    saved = {
        "is_traded": True,
        "is_flattening": True,
        "active_side": "LONG",
        "active_exec_symbol": "TQQQ",
        "pending_decision_id": "d1",
        "active_trigger_id": "t1",
        "expected_entry_qty": 8,
        "entry_order_ref": "oref",
        "entry_px": "50.0",
        "stop_px": "48.5",
        "entry_atr_used": "0.7",
        "tp1_hit": True,
        "trail_high_px": "51.0",
        "trail_floor_px": "49.9",
        "breakeven_done": True,
    }
    restore_runtime_state_row(
        state=state,
        saved=saved,
        optional_float=lambda v: float(v) if v is not None else None,
    )
    assert state.is_traded is True
    assert state.is_flattening is True
    assert state.active_side == "LONG"
    assert state.active_exec_symbol == "TQQQ"
    assert state.pending_decision_id == "d1"
    assert state.active_trigger_id == "t1"
    assert state.expected_entry_qty == 8
    assert state.entry_order_ref == "oref"
    assert state.entry_px == 50.0
    assert state.stop_px == 48.5
    assert state.entry_atr_used == 0.7
    assert state.tp1_hit is True
    assert state.trail_high_px == 51.0
    assert state.trail_floor_px == 49.9
    assert state.breakeven_done is True


def test_audit_context_helpers() -> None:
    cache: dict[str, dict[str, str]] = {}
    remember_audit_context(cache, symbol="qqq", decision_id="d1")
    ctx = ensure_audit_context(cache, symbol="qqq", make_decision_id=lambda: "gen")
    assert ctx["decision_id"] == "d1"
    assert ctx["trace_id"] == "d1"
    assert ctx["trigger_id"]

    decision_id, trigger_id, trace_id = resolve_audit_ids(ctx)
    assert decision_id == "d1"
    assert trigger_id
    assert trace_id == "d1"
