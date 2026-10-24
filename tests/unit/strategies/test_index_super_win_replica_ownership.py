from __future__ import annotations

from datetime import datetime

from strategies.index_super_win_replica_ownership import StockOwnershipLedger
from strategies.index_super_win_replica_ownership_types import (
    ReplicaExitFillRecord,
    ReplicaOwnershipIntentRecord,
)


def test_stock_ownership_ledger_distinguishes_same_symbol_across_strategies():
    ledger = StockOwnershipLedger()
    ts_us = int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000)
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=8.0,
            ts_bar_end_us=ts_us,
            position_lot_id="lot:s1:1",
            order_ref="oref1",
        )
    )
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s2",
            decision_id="d2",
            trigger_id="t2",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=8.0,
            ts_bar_end_us=ts_us,
            position_lot_id="lot:s2:1",
            order_ref="oref2",
        )
    )
    ledger.mark_entry_live(position_lot_id="lot:s1:1", filled_qty=8.0)
    ledger.mark_entry_live(position_lot_id="lot:s2:1", filled_qty=8.0)

    active = ledger.active_lots(exec_symbol="TQQQ")
    assert len(active) == 2
    assert {lot.strategy_id for lot in active} == {"s1", "s2"}


def test_stock_ownership_ledger_distinguishes_multiple_lots_same_strategy():
    ledger = StockOwnershipLedger()
    ts_us = int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000)
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=8.0,
            ts_bar_end_us=ts_us,
            position_lot_id="lot:s1:1",
            order_ref="oref1",
        )
    )
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d2",
            trigger_id="t2",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=4.0,
            ts_bar_end_us=ts_us + 60_000_000,
            position_lot_id="lot:s1:2",
            order_ref="oref2",
        )
    )
    ledger.mark_entry_live(position_lot_id="lot:s1:1", filled_qty=8.0)
    ledger.mark_entry_live(position_lot_id="lot:s1:2", filled_qty=4.0)

    active = ledger.active_lots(strategy_id="s1", exec_symbol="TQQQ")
    assert len(active) == 2
    assert {lot.position_lot_id for lot in active} == {"lot:s1:1", "lot:s1:2"}


def test_stock_ownership_ledger_attaches_broker_identity_and_resolves_lot():
    ledger = StockOwnershipLedger()
    ts_us = int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000)
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=8.0,
            ts_bar_end_us=ts_us,
            position_lot_id="lot:s1:1",
            order_ref="oref1",
        )
    )
    ledger.attach_order_identity(position_lot_id="lot:s1:1", broker_order_id="101", perm_id=555)

    by_broker = ledger.lot_for_identity(broker_order_id="101")
    by_perm = ledger.lot_for_identity(perm_id=555)
    assert by_broker is not None
    assert by_perm is not None
    assert by_broker.position_lot_id == "lot:s1:1"
    assert by_perm.position_lot_id == "lot:s1:1"


def test_stock_ownership_ledger_partial_exit_only_reduces_owned_lot():
    ledger = StockOwnershipLedger()
    ts = datetime(2026, 3, 25, 11, 11, 0)
    ts_us = int(ts.timestamp() * 1_000_000)
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=8.0,
            ts_bar_end_us=ts_us,
            position_lot_id="lot:s1:1",
            order_ref="oref1",
        )
    )
    ledger.mark_entry_live(position_lot_id="lot:s1:1", filled_qty=8.0)
    ledger.record_exit_fill(
        ReplicaExitFillRecord(
            strategy_id="s1",
            position_lot_id="lot:s1:1",
            qty=4.0,
            reason="tp1",
            ts=ts,
            order_ref="tp1ref",
            broker_order_id="202",
            perm_id=777,
        )
    )

    active = ledger.active_lots(strategy_id="s1", exec_symbol="TQQQ")
    assert len(active) == 1
    assert active[0].remaining_qty == 4.0
    assert active[0].status == "partial_exit"


def test_stock_ownership_ledger_snapshot_round_trip_preserves_active_qty():
    ledger = StockOwnershipLedger()
    ts = datetime(2026, 3, 25, 11, 11, 0)
    ts_us = int(ts.timestamp() * 1_000_000)
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=8.0,
            ts_bar_end_us=ts_us,
            position_lot_id="lot:s1:1",
            order_ref="oref1",
        )
    )
    ledger.mark_entry_live(position_lot_id="lot:s1:1", filled_qty=8.0)
    payload = ledger.snapshot()

    restored = StockOwnershipLedger.from_snapshot(payload)

    active = restored.active_lots(strategy_id="s1", exec_symbol="TQQQ")
    assert len(active) == 1
    assert active[0].position_lot_id == "lot:s1:1"
    assert active[0].remaining_qty == 8.0
    assert isinstance(active[0].opened_at, datetime)


def test_stock_ownership_ledger_allocate_exit_fill_splits_across_multiple_lots():
    ledger = StockOwnershipLedger()
    ts = datetime(2026, 3, 25, 11, 11, 0)
    ts_us = int(ts.timestamp() * 1_000_000)
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            trigger_id="t1",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=4.0,
            ts_bar_end_us=ts_us,
            position_lot_id="lot:s1:1",
            order_ref="oref1",
        )
    )
    ledger.record_entry_intent(
        ReplicaOwnershipIntentRecord(
            strategy_id="s1",
            decision_id="d2",
            trigger_id="t2",
            signal_symbol="QQQ",
            exec_symbol="TQQQ",
            side="LONG",
            qty=4.0,
            ts_bar_end_us=ts_us + 60_000_000,
            position_lot_id="lot:s1:2",
            order_ref="oref2",
        )
    )
    ledger.mark_entry_live(position_lot_id="lot:s1:1", filled_qty=4.0)
    ledger.mark_entry_live(position_lot_id="lot:s1:2", filled_qty=4.0)

    allocated = ledger.allocate_exit_fill(
        strategy_id="s1",
        signal_symbol="QQQ",
        exec_symbol="TQQQ",
        qty=6.0,
        reason="flatten",
        ts=ts,
        order_ref="flatten-ref",
    )

    assert allocated == 6.0
    active = ledger.active_lots(strategy_id="s1", exec_symbol="TQQQ")
    assert len(active) == 1
    assert active[0].position_lot_id == "lot:s1:2"
    assert active[0].remaining_qty == 2.0
