from types import SimpleNamespace

from strategies.stocks_kernel.modules.execution_semantics import ExecutionSemanticsModule
from strategies.stocks_kernel.types import FeatureContext, HistoryBundle, MarketSnapshot


def _lot(**kwargs):
    defaults = {
        "position_lot_id": "lot-1",
        "symbol": "TQQQ",
        "side": "LONG",
        "state": "live",
        "live_qty": 2.0,
        "avg_entry_price": 100.0,
        "flatten_order_refs": [],
        "extras": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_execution_semantics_should_skip_exit_for_lot():
    module = ExecutionSemanticsModule(strategy=SimpleNamespace())
    assert module.should_skip_exit_for_lot(_lot(state="pending_entry")) is True
    assert module.should_skip_exit_for_lot(_lot(flatten_order_refs=["o1"])) is True
    assert module.should_skip_exit_for_lot(_lot(live_qty=0.0)) is True
    assert module.should_skip_exit_for_lot(_lot()) is False


def test_execution_semantics_evaluate_entry_conflict():
    module = ExecutionSemanticsModule(strategy=SimpleNamespace())
    ready = module.evaluate_entry_conflict(entry_symbol="TQQQ", active_binding_lots=[])
    assert ready.blocked is False
    conflict = module.evaluate_entry_conflict(
        entry_symbol="TQQQ",
        active_binding_lots=[_lot(position_lot_id="lot-a", symbol="TQQQ"), _lot(position_lot_id="lot-b", symbol="SQQQ")],
    )
    assert conflict.blocked is True
    assert conflict.reason == "existing_exec_lot_present"
    assert sorted(conflict.payload["active_symbols"]) == ["SQQQ", "TQQQ"]
    assert sorted(conflict.payload["active_position_lot_ids"]) == ["lot-a"]


def test_execution_semantics_evaluate_entry_conflict_allows_cross_strategy_lane_scope():
    module = ExecutionSemanticsModule(strategy=SimpleNamespace())
    ready = module.evaluate_entry_conflict(
        entry_symbol="TQQQ",
        strategy_id="beta",
        entry_lane_id="beta_long",
        entry_ownership_scope="lane",
        active_binding_lots=[
            _lot(position_lot_id="lot-a", symbol="TQQQ", strategy_id="alpha", lane_id="alpha_long", ownership_scope="lane"),
        ],
    )

    assert ready.blocked is False
    assert ready.reason == "ready"


def test_execution_semantics_pending_entry_quarantine_candidate():
    module = ExecutionSemanticsModule(strategy=SimpleNamespace())
    lot = _lot(state="pending_entry", extras={"entry_requested_ts_epoch_s": 10.0})
    should_quarantine, detail = module.pending_entry_quarantine_candidate(
        lot=lot,
        now_ts_epoch_s=40.0,
        timeout_sec=20.0,
    )
    assert should_quarantine is True
    assert detail["age_sec"] == 30.0
    assert detail["timeout_sec"] == 20.0


def test_execution_semantics_select_exit_plan_priority_stop_then_tp_then_retreat():
    module = ExecutionSemanticsModule(strategy=SimpleNamespace())
    feature_context = FeatureContext(
        symbol="QQQ",
        snapshot=MarketSnapshot(symbol="QQQ", ts_epoch_s=200.0, last_price=95.0),
        history=HistoryBundle(symbol="QQQ"),
        values={"v3m": -0.5},
        readiness={},
    )
    lot = _lot(
        symbol="TQQQ",
        side="LONG",
        live_qty=1.0,
        avg_entry_price=100.0,
        extras={
            "entry_ts_epoch_s": 0.0,
            "stop_plan": {"logic_stop_offset": 2.0},
            "take_profit_plan": {"atr_eff": 2.0, "target_offset_atr": 1.0},
            "retreat_plan": {"logic_retreat_min_hold_sec": 10.0},
        },
    )
    snapshot = MarketSnapshot(symbol="TQQQ", ts_epoch_s=200.0, last_price=97.5)
    plan = module.select_exit_plan_for_lot(lot=lot, snapshot=snapshot, feature_context=feature_context)
    assert plan is not None
    assert plan.order_role == "STOP"
