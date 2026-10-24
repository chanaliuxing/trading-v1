from __future__ import annotations

from datetime import datetime

from libs.strategy import OrderUpdate
from strategies.index_super_win_replica_core import ET, ReplicaEntryCore
from strategies.index_super_win_replica_types import ReplicaTradeState


def _state() -> ReplicaTradeState:
    return ReplicaTradeState(
        signal_symbol="QQQ",
        exec_symbol_long="TQQQ",
        exec_symbol_short="SQQQ",
    )


def test_build_entry_intent_uses_side_specific_exec_symbol() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, intent, envelope = core.build_entry_intent(
        state=_state(),
        side="SHORT",
        qty=7,
        reason="entry_allowed",
        trigger_id="abc123",
        ref_px=500.0,
        ref_px_source="signal_price",
    )

    assert state.active_exec_symbol == "SQQQ"
    assert intent.symbol == "SQQQ"
    assert envelope.symbol == "SQQQ"
    assert envelope.target_qty == -7.0
    assert ":SQQQ:ENTRY:TRG:abc123" in intent.order_ref


def test_handle_submit_result_rejected_clears_pending_state() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, _, _ = core.build_entry_intent(
        state=_state(),
        side="LONG",
        qty=5,
        reason="entry_allowed",
        trigger_id="trg1",
    )

    state, decision = core.handle_submit_result(state=state, submit_result={"status": "REJECTED", "reasons": ["risk_blocked"]})
    assert decision.accepted is False
    assert decision.event == "entry_intent_rejected"
    assert state.pending_decision_id is None
    assert state.entry_order_ref is None
    assert state.expected_entry_qty == 0


def test_handle_entry_order_update_filled_marks_live() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, intent, _ = core.build_entry_intent(
        state=_state(),
        side="LONG",
        qty=5,
        reason="entry_allowed",
        trigger_id="trg1",
    )
    update = OrderUpdate(
        order_id="1",
        strategy_id="index_super_win_replica_001",
        symbol="TQQQ",
        state="FILLED",
        order_ref=intent.order_ref,
    )

    state, decision = core.handle_entry_order_update(state=state, update=update)
    assert decision.accepted is True
    assert decision.event == "entry_confirmed"
    assert state.is_traded is True
    assert state.pending_decision_id is None


def test_handle_entry_order_update_rejects_foreign_order_ref() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    update = OrderUpdate(
        order_id="1",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        state="FILLED",
        order_ref="qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:foreign",
    )

    state, decision = core.handle_entry_order_update(state=state, update=update)
    assert decision.accepted is False
    assert decision.reason == "foreign_order_ref"


def test_build_protective_stop_intent_uses_runtime_qty_hint_and_stop_px() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, _, _ = core.build_entry_intent(
        state=_state(),
        side="LONG",
        qty=5,
        reason="entry_allowed",
        trigger_id="trg1",
    )
    state.is_traded = True

    state, intent, envelope = core.build_protective_stop_intent(
        state=state,
        qty_signed=5.0,
        reason="entry_protective_stop",
        stop_px=47.04,
    )

    assert intent.kind == "STOP"
    assert intent.stop_px == 47.04
    assert envelope.extras.get("protective_stop") is True
    assert float(envelope.extras.get("position_qty_hint")) == 5.0
    assert envelope.extras.get("position_qty_hint_source") == "strategy_runtime"
    assert envelope.order_style.stop_px == 47.04


def test_build_protective_stop_intent_supports_short_inventory() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, _, _ = core.build_entry_intent(
        state=_state(),
        side="SHORT",
        qty=5,
        reason="entry_allowed",
        trigger_id="trg1",
    )
    state.is_traded = True

    _, intent, envelope = core.build_protective_stop_intent(
        state=state,
        qty_signed=-5.0,
        reason="entry_protective_stop",
        stop_px=53.21,
    )

    assert intent.symbol == "SQQQ"
    assert intent.target_qty == 5.0
    assert envelope.target_qty == 5.0
    assert float(envelope.extras.get("position_qty_hint")) == -5.0


def test_build_protective_trailing_intent_uses_native_trail_style() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, _, _ = core.build_entry_intent(
        state=_state(),
        side="LONG",
        qty=5,
        reason="entry_allowed",
        trigger_id="trg1",
    )
    state.is_traded = True
    state.stop_px = 47.04

    state, intent, envelope = core.build_protective_trailing_intent(
        state=state,
        qty_signed=3.0,
        reason="tp1_runner_native_trail",
        trail_percent=1.5,
        trail_stop_px=49.12,
    )

    assert intent.kind == "TRAIL"
    assert intent.trail_percent == 1.5
    assert intent.trail_stop_px == 49.12
    assert envelope.order_style.kind == "TRAIL"
    assert envelope.order_style.close_only is True
    assert envelope.extras["runner_trail"] is True
    assert envelope.extras["previous_stop_px"] == 47.04
    assert state.stop_px == 47.04
    assert state.extras.get("native_trail_active") is not True


def test_build_flatten_intent_marks_flattening_and_uses_close_only() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, _, _ = core.build_entry_intent(
        state=_state(),
        side="LONG",
        qty=5,
        reason="entry_allowed",
        trigger_id="trg1",
    )
    state.is_traded = True

    state, intent, envelope = core.build_flatten_intent(
        state=state,
        qty_signed=5.0,
        reason="logic_retreat",
    )

    assert state.is_flattening is True
    assert intent.kind == "FLATTEN"
    assert intent.target_qty == -5.0
    assert envelope.extras.get("flatten_action") is True
    assert envelope.extras.get("close_only") is True
    assert float(envelope.extras.get("position_qty_hint")) == 5.0


def test_build_tp1_partial_exit_intent_uses_close_only_and_tp1_order_ref() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state, _, _ = core.build_entry_intent(
        state=_state(),
        side="LONG",
        qty=8,
        reason="entry_allowed",
        trigger_id="trg1",
    )
    state.is_traded = True

    state, intent, envelope = core.build_tp1_partial_exit_intent(
        state=state,
        qty_signed=8.0,
        qty_to_exit=4,
        reason="tp1_hit",
    )

    assert intent.kind == "TP1"
    assert intent.target_qty == -4.0
    assert ":TP1:TRG:trg1" in intent.order_ref
    assert envelope.extras.get("tp1_partial_exit") is True
    assert envelope.extras.get("close_only") is True


def test_evaluate_position_management_triggers_tp1_then_trailing_stop() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.extras["trade_target_px"] = 52.0
    state.extras["entry_time_et"] = __import__("datetime").datetime(2026, 3, 16, 10, 0, 0)

    state, first_decision = core.evaluate_position_management(
        state=state,
        now=__import__("datetime").datetime(2026, 3, 16, 10, 10, 0),
        last_px=52.2,
        live_qty=10,
    )
    assert first_decision.action == "tp1_exit"
    assert first_decision.reason == "tp1_hit"
    assert state.tp1_hit is True

    state, second_decision = core.evaluate_position_management(
        state=state,
        now=__import__("datetime").datetime(2026, 3, 16, 10, 15, 0),
        last_px=50.8,
        live_qty=5,
    )
    assert second_decision.action == "flatten"
    assert second_decision.reason == "tp1_then_trailing_stop"


def test_evaluate_position_management_triggers_breakeven_shield() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.extras["entry_time_et"] = __import__("datetime").datetime(2026, 3, 16, 10, 0, 0)

    state, decision = core.evaluate_position_management(
        state=state,
        now=__import__("datetime").datetime(2026, 3, 16, 10, 20, 0),
        last_px=51.5,
        live_qty=10,
        enable_breakeven_shield=True,
    )
    assert decision.action == "move_stop"
    assert decision.reason == "breakeven_shield"
    assert decision.updated_stop_px == 50.2
    assert state.breakeven_done is True


def test_evaluate_position_management_triggers_force_eod_flatten() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.extras["entry_time_et"] = __import__("datetime").datetime(2026, 3, 16, 10, 0, 0)

    state, decision = core.evaluate_position_management(
        state=state,
        now=__import__("datetime").datetime(2026, 3, 16, 15, 59, 0),
        last_px=50.1,
        live_qty=10,
    )
    assert decision.action == "flatten"
    assert decision.reason == "force_flatten_1559_EOD"


def test_evaluate_position_management_short_triggers_tp1_then_trailing_stop() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "SHORT"
    state.active_exec_symbol = "SQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.extras["trade_target_px"] = 48.0
    state.extras["entry_time_et"] = __import__("datetime").datetime(2026, 3, 16, 10, 0, 0)

    state, first_decision = core.evaluate_position_management(
        state=state,
        now=__import__("datetime").datetime(2026, 3, 16, 10, 10, 0),
        last_px=47.8,
        live_qty=10,
    )
    assert first_decision.action == "tp1_exit"
    assert first_decision.reason == "tp1_hit"
    assert state.tp1_hit is True
    assert state.trail_floor_px == 49.6

    state, second_decision = core.evaluate_position_management(
        state=state,
        now=__import__("datetime").datetime(2026, 3, 16, 10, 15, 0),
        last_px=49.2,
        live_qty=5,
    )
    assert second_decision.action == "flatten"
    assert second_decision.reason == "tp1_then_trailing_stop"


def test_evaluate_position_management_triggers_logic_retreat_long() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    entry_time = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    state = ReplicaTradeState(
        signal_symbol="QQQ",
        exec_symbol_long="TQQQ",
        exec_symbol_short="SQQQ",
        active_side="LONG",
        active_exec_symbol="TQQQ",
        is_traded=True,
        entry_px=50.0,
    )
    state.extras["entry_time_et"] = entry_time

    _, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 8, tzinfo=ET),
        last_px=50.2,
        live_qty=8,
        primary_px=499.0,
        primary_vwap=500.0,
        primary_age_sec=2.0,
        qqq_px=499.0,
        qqq_vwap=500.0,
        qqq_age_sec=2.0,
        enable_logic_retreat=True,
        logic_retreat_min_hold_sec=420.0,
    )

    assert decision.action == "flatten"
    assert decision.reason == "logic_retreat"


def test_evaluate_position_management_records_logic_retreat_stale_minute() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    entry_time = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    state = ReplicaTradeState(
        signal_symbol="QQQ",
        exec_symbol_long="TQQQ",
        exec_symbol_short="SQQQ",
        active_side="LONG",
        active_exec_symbol="TQQQ",
        is_traded=True,
        entry_px=50.0,
    )
    state.extras["entry_time_et"] = entry_time

    state, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 8, 33, tzinfo=ET),
        last_px=50.2,
        live_qty=8,
        primary_px=499.0,
        primary_vwap=500.0,
        primary_age_sec=25.0,
        qqq_px=499.0,
        qqq_vwap=500.0,
        qqq_age_sec=25.0,
        enable_logic_retreat=True,
        logic_retreat_min_hold_sec=420.0,
        logic_retreat_max_primary_age_sec=15.0,
        logic_retreat_max_qqq_age_sec=15.0,
    )

    assert decision.action == "noop"
    assert decision.reason == "hold"
    assert state.extras["last_logic_retreat_minute"] == datetime(2026, 3, 16, 10, 8, 0, tzinfo=ET)


def test_evaluate_position_management_triggers_add_position_long() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    entry_time = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    state = ReplicaTradeState(
        signal_symbol="QQQ",
        exec_symbol_long="TQQQ",
        exec_symbol_short="SQQQ",
        active_side="LONG",
        active_exec_symbol="TQQQ",
        is_traded=True,
        entry_px=50.0,
        entry_atr_used=2.0,
    )
    state.extras["entry_time_et"] = entry_time
    state.extras["planned_probe_qty"] = 4
    state.extras["planned_add_qty"] = 4
    state.extras["add_done"] = False

    state, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 1, tzinfo=ET),
        last_px=50.3,
        live_qty=4,
        enable_add_on=True,
    )

    assert decision.action == "add_position"
    assert decision.reason == "primary_intraday_add"
    assert decision.add_qty == 4
    assert decision.updated_entry_px is not None


def test_evaluate_position_management_triggers_add_position_short() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    entry_time = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    state = ReplicaTradeState(
        signal_symbol="QQQ",
        exec_symbol_long="TQQQ",
        exec_symbol_short="SQQQ",
        active_side="SHORT",
        active_exec_symbol="SQQQ",
        is_traded=True,
        entry_px=50.0,
        entry_atr_used=2.0,
    )
    state.extras["entry_time_et"] = entry_time
    state.extras["planned_probe_qty"] = 4
    state.extras["planned_add_qty"] = 4
    state.extras["add_done"] = False

    _, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 1, tzinfo=ET),
        last_px=49.7,
        live_qty=4,
        enable_add_on=True,
    )

    assert decision.action == "add_position"
    assert decision.reason == "primary_intraday_add"
    assert decision.add_qty == 4


def test_evaluate_position_management_add_position_rate_limited_per_minute() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    entry_time = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    state = ReplicaTradeState(
        signal_symbol="QQQ",
        exec_symbol_long="TQQQ",
        exec_symbol_short="SQQQ",
        active_side="LONG",
        active_exec_symbol="TQQQ",
        is_traded=True,
        entry_px=50.0,
        entry_atr_used=2.0,
    )
    state.extras["entry_time_et"] = entry_time
    state.extras["planned_probe_qty"] = 4
    state.extras["planned_add_qty"] = 4
    state.extras["add_done"] = False

    state, d1 = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 1, 20, tzinfo=ET),
        last_px=50.3,
        live_qty=4,
        enable_add_on=True,
    )
    assert d1.action == "add_position"
    assert d1.reason == "primary_intraday_add"


def test_evaluate_position_management_supports_primary_signal_inputs_without_qqq_aliases() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    entry_time = datetime(2026, 3, 16, 10, 0, tzinfo=ET)
    state = ReplicaTradeState(
        signal_symbol="SPY",
        exec_symbol_long="UPRO",
        exec_symbol_short="SPXU",
        active_side="LONG",
        active_exec_symbol="UPRO",
        is_traded=True,
        entry_px=50.0,
    )
    state.extras["entry_time_et"] = entry_time

    _, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 8, tzinfo=ET),
        last_px=50.2,
        live_qty=8,
        primary_px=499.0,
        primary_vwap=500.0,
        primary_age_sec=2.0,
        enable_logic_retreat=True,
        logic_retreat_min_hold_sec=420.0,
        logic_retreat_max_primary_age_sec=15.0,
    )

    assert decision.action == "flatten"
    assert decision.reason == "logic_retreat"

    state, d2 = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 1, 40, tzinfo=ET),
        last_px=50.35,
        live_qty=4,
        enable_add_on=True,
    )
    assert d2.action == "noop"
    assert d2.reason == "hold"


def test_evaluate_position_management_triggers_dynamic_stop_widen_long() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.stop_px = 45.0
    state.entry_atr_used = 2.0
    state.extras["stop_dist_pre"] = 5.0
    state.extras["stop_dist_cur"] = 5.0

    state, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 20, 0),
        last_px=51.0,
        live_qty=10,
        atr_live=3.0,
        enable_dynamic_stop_widen=True,
    )
    assert decision.action == "move_stop"
    assert decision.reason == "stop_widened"
    assert decision.updated_stop_px == 42.5
    assert state.extras["stop_dist_cur"] == 7.5


def test_evaluate_position_management_records_tp1_eod_no_exit_minute() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.tp1_hit = True
    state.trail_high_px = 52.0
    state.trail_floor_px = 50.4
    state.extras["trade_target_px"] = 52.0

    state, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 20, 30, tzinfo=ET),
        last_px=52.1,
        live_qty=5,
        enable_hard_stop=True,
        eod_flatten_time_hhmm=(15, 59, 0),
    )
    assert decision.action == "noop"
    assert decision.reason == "hold"
    assert state.extras["last_tp1_eod_no_exit_minute"] == datetime(2026, 3, 16, 10, 20, 0, tzinfo=ET)



def test_evaluate_position_management_triggers_dynamic_stop_widen_short() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "SHORT"
    state.active_exec_symbol = "SQQQ"
    state.entry_px = 50.0
    state.stop_px = 55.0
    state.entry_atr_used = 2.0
    state.extras["stop_dist_pre"] = 5.0
    state.extras["stop_dist_cur"] = 5.0

    state, decision = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 20, 0),
        last_px=49.0,
        live_qty=10,
        atr_live=3.0,
        enable_dynamic_stop_widen=True,
    )
    assert decision.action == "move_stop"
    assert decision.reason == "stop_widened"
    assert decision.updated_stop_px == 57.5
    assert state.extras["stop_dist_cur"] == 7.5


def test_evaluate_position_management_dynamic_stop_widen_is_rate_limited_per_5m() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "LONG"
    state.active_exec_symbol = "TQQQ"
    state.entry_px = 50.0
    state.stop_px = 45.0
    state.entry_atr_used = 2.0
    state.extras["stop_dist_pre"] = 5.0
    state.extras["stop_dist_cur"] = 5.0

    state, d1 = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 20, 0),
        last_px=51.0,
        live_qty=10,
        atr_live=2.2,
        enable_dynamic_stop_widen=True,
    )
    assert d1.action == "move_stop"
    assert d1.updated_stop_px == 44.5
    assert state.extras["stop_dist_cur"] == 5.5

    state, d2 = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 21, 0),
        last_px=51.0,
        live_qty=10,
        atr_live=2.6,
        enable_dynamic_stop_widen=True,
    )
    assert d2.action == "noop"
    assert d2.reason == "hold"
    assert state.extras["stop_dist_cur"] == 5.5

    state, d3 = core.evaluate_position_management(
        state=state,
        now=datetime(2026, 3, 16, 10, 26, 0),
        last_px=51.0,
        live_qty=10,
        atr_live=2.6,
        enable_dynamic_stop_widen=True,
    )
    assert d3.action == "move_stop"
    assert d3.updated_stop_px == 43.5
    assert state.extras["stop_dist_cur"] == 6.5


def test_evaluate_position_management_short_triggers_breakeven_shield() -> None:
    core = ReplicaEntryCore(strategy_id="index_super_win_replica_001", order_namespace="iswv2")
    state = _state()
    state.is_traded = True
    state.active_side = "SHORT"
    state.active_exec_symbol = "SQQQ"
    state.entry_px = 50.0
    state.entry_atr_used = 2.0
    state.extras["entry_time_et"] = __import__("datetime").datetime(2026, 3, 16, 10, 0, 0)

    state, decision = core.evaluate_position_management(
        state=state,
        now=__import__("datetime").datetime(2026, 3, 16, 10, 20, 0),
        last_px=48.5,
        live_qty=10,
        enable_breakeven_shield=True,
    )
    assert decision.action == "move_stop"
    assert decision.reason == "breakeven_shield"
    assert decision.updated_stop_px == 49.8
    assert state.breakeven_done is True
