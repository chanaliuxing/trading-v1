from types import SimpleNamespace

from strategies.stocks_kernel.runtime.submit_bridge import StrategySubmitBridge
from strategies.stocks_kernel.types import ActionPlan, ActionType, ExitPlan


def test_submit_bridge_falls_back_when_snapshot_timestamp_missing():
    bridge = StrategySubmitBridge(strategy_id="alpha", strategy_run_id="run-1", ttl_ms=2000, dry_run=False)
    snapshot = SimpleNamespace(ts_epoch_s=0.0)
    entry_plan = SimpleNamespace(qty=1.0, side="LONG", trigger_id="t-1")

    envelope = bridge.build_envelope(symbol="SOXL", entry_plan=entry_plan, snapshot=snapshot)

    assert envelope.ts_bar_end_us > 0
    assert ":SOXL:" in envelope.decision_id


def test_submit_bridge_entry_decision_id_includes_trigger_token_for_same_tick_uniqueness():
    bridge = StrategySubmitBridge(strategy_id="alpha", strategy_run_id="run-1", ttl_ms=2000, dry_run=False)
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    entry_a = SimpleNamespace(qty=1.0, side="LONG", trigger_id="entry-A", lane_id=None, payload={"action_type": "ENTRY"})
    entry_b = SimpleNamespace(qty=1.0, side="LONG", trigger_id="add/B", lane_id=None, payload={"action_type": "ADD"})

    env_a = bridge.build_envelope(symbol="SOXL", entry_plan=entry_a, snapshot=snapshot)
    env_b = bridge.build_envelope(symbol="SOXL", entry_plan=entry_b, snapshot=snapshot)

    assert env_a.decision_id != env_b.decision_id
    assert ":trg-entry-a:" in env_a.decision_id
    assert ":trg-add_b:" in env_b.decision_id
    assert ":act-entry:" in env_a.decision_id
    assert ":act-add:" in env_b.decision_id
    assert env_a.decision_id.endswith(":qty-1")
    assert env_b.decision_id.endswith(":qty-1")


def test_submit_bridge_entry_decision_id_stable_for_same_trigger_and_unique_by_qty():
    bridge = StrategySubmitBridge(strategy_id="alpha", strategy_run_id="run-1", ttl_ms=2000, dry_run=False)
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    entry_a = SimpleNamespace(qty=1.0, side="LONG", trigger_id="same-trigger", lane_id="long_lane", payload={"action_type": "ENTRY"})
    entry_b = SimpleNamespace(qty=2.0, side="LONG", trigger_id="same-trigger", lane_id="long_lane", payload={"action_type": "ENTRY"})

    env_a = bridge.build_envelope(symbol="SOXL", entry_plan=entry_a, snapshot=snapshot)
    env_b = bridge.build_envelope(symbol="SOXL", entry_plan=entry_b, snapshot=snapshot)

    assert env_a.decision_id != env_b.decision_id
    assert env_a.decision_id.endswith(":qty-1")
    assert env_b.decision_id.endswith(":qty-2")


def test_submit_bridge_build_action_envelope_preserves_execution_semantics_flags():
    bridge = StrategySubmitBridge(strategy_id="alpha", strategy_run_id="run-1", ttl_ms=2000, dry_run=False)
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    action = ActionPlan(
        action_type=ActionType.FLATTEN,
        symbol="SOXL",
        qty=10.0,
        close_only=True,
        flatten_action=True,
        reduce_only=True,
        reason="eod_flatten",
        payload={"position_lot_id": "lot-1"},
    )

    envelope = bridge.build_action_envelope(action_plan=action, snapshot=snapshot)

    assert envelope.symbol == "SOXL"
    assert envelope.target_qty == 0.0
    assert envelope.extras["action_type"] == "FLATTEN"
    assert envelope.extras["close_only"] is True
    assert envelope.extras["flatten_action"] is True
    assert envelope.extras["reduce_only"] is True


def test_submit_bridge_action_decision_id_unique_for_same_tick_by_trigger_lane_and_qty():
    bridge = StrategySubmitBridge(strategy_id="alpha", strategy_run_id="run-1", ttl_ms=2000, dry_run=False)
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    action_a = ActionPlan(
        action_type=ActionType.ENTRY,
        symbol="SOXL",
        side="LONG",
        qty=1.0,
        trigger_id="trg-a",
        lane_id="lane_a",
    )
    action_b = ActionPlan(
        action_type=ActionType.ENTRY,
        symbol="SOXL",
        side="LONG",
        qty=2.0,
        trigger_id="trg-b",
        lane_id="lane_b",
    )

    env_a = bridge.build_action_envelope(action_plan=action_a, snapshot=snapshot)
    env_b = bridge.build_action_envelope(action_plan=action_b, snapshot=snapshot)

    assert env_a.decision_id != env_b.decision_id
    assert ":trg-trg-a:" in env_a.decision_id
    assert ":lane-lane_a:" in env_a.decision_id
    assert env_a.decision_id.endswith(":qty-1")
    assert env_b.decision_id.endswith(":qty-2")


def test_submit_bridge_action_decision_id_v2_can_be_disabled_for_legacy_fallback():
    bridge = StrategySubmitBridge(
        strategy_id="alpha",
        strategy_run_id="run-1",
        ttl_ms=2000,
        dry_run=False,
        action_decision_id_v2_enabled=False,
    )
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    action = ActionPlan(
        action_type=ActionType.ENTRY,
        symbol="SOXL",
        side="LONG",
        qty=1.0,
        trigger_id="trg-a",
        lane_id="lane_a",
    )

    env = bridge.build_action_envelope(action_plan=action, snapshot=snapshot)

    assert env.decision_id == "alpha:SOXL:100000000:entry"


def test_submit_bridge_exit_envelope_sets_close_only_and_flatten_flags():
    bridge = StrategySubmitBridge(strategy_id="alpha", strategy_run_id="run-1", ttl_ms=2000, dry_run=False)
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    lot = SimpleNamespace(position_lot_id="lot-1", side="LONG", live_qty=12.0)
    exit_plan = ExitPlan(symbol="SOXL", order_role="FLATTEN", qty=12.0, position_lot_id="lot-1", payload={})

    envelope = bridge.build_exit_envelope(exit_plan=exit_plan, snapshot=snapshot, lot=lot)

    assert envelope.target_qty == -12.0
    assert envelope.extras["exit_order_role"] == "FLATTEN"
    assert envelope.extras["close_only"] is True
    assert envelope.extras["flatten_action"] is True
    assert envelope.extras["reduce_only"] is True
    assert envelope.extras["position_qty_hint"] == 12.0
    assert envelope.extras["position_qty_hint_source"] == "strategy_runtime"


def test_submit_bridge_exit_envelope_uses_lot_qty_fallback_when_exit_qty_missing():
    bridge = StrategySubmitBridge(strategy_id="alpha", strategy_run_id="run-1", ttl_ms=2000, dry_run=False)
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    lot = SimpleNamespace(position_lot_id="lot-2", side="SHORT", live_qty=7.0, filled_qty=7.0)
    exit_plan = ExitPlan(symbol="SOXS", order_role="STOP", qty=0.0, position_lot_id="lot-2", payload={})

    envelope = bridge.build_exit_envelope(exit_plan=exit_plan, snapshot=snapshot, lot=lot)

    assert envelope.target_qty == 7.0
    assert envelope.extras["position_qty_hint"] == -7.0
    assert envelope.extras["exit_qty"] == 7.0


def test_submit_bridge_exit_envelope_inherits_lot_strategy_instance_owner():
    bridge = StrategySubmitBridge(
        strategy_id="alpha",
        strategy_instance_id="alpha_default",
        strategy_run_id="run-1",
        ttl_ms=2000,
        dry_run=False,
    )
    snapshot = SimpleNamespace(ts_epoch_s=100.0)
    lot = SimpleNamespace(
        position_lot_id="lot-3",
        side="LONG",
        live_qty=5.0,
        extras={"strategy_instance_id": "alpha_restart_001", "account_id": "DU123"},
    )
    exit_plan = ExitPlan(symbol="SOXL", order_role="FLATTEN", qty=5.0, position_lot_id="lot-3", payload={})

    envelope = bridge.build_exit_envelope(exit_plan=exit_plan, snapshot=snapshot, lot=lot)

    assert envelope.extras["strategy_instance_id"] == "alpha_restart_001"
    assert envelope.extras["position_lot_id"] == "lot-3"
    assert envelope.extras["lot_id"] == "lot-3"
    assert envelope.extras["position_owner_key"] == "alpha_restart_001:SOXL:lot-3"
