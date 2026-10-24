from datetime import datetime

from strategies.options_kernel.ownership import OptionOwnershipLedger
from strategies.options_kernel.types import KernelIntentRecord


def test_ownership_ledger_distinguishes_same_symbol_across_strategies():
    ledger = OptionOwnershipLedger()
    ts_us = int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000)
    ledger.record_intent(
        KernelIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            symbol="OPT:QQQ:20260326:590:P",
            target_qty=4.0,
            ts_bar_end_us=ts_us,
            order_ref="oref1",
            position_lot_id="lot:s1:1",
        )
    )
    ledger.record_intent(
        KernelIntentRecord(
            strategy_id="s2",
            decision_id="d2",
            symbol="OPT:QQQ:20260326:590:P",
            target_qty=4.0,
            ts_bar_end_us=ts_us,
            order_ref="oref2",
            position_lot_id="lot:s2:1",
        )
    )

    pending = ledger.pending_entry_lots(symbol="OPT:QQQ:20260326:590:P")
    assert len(pending) == 2
    assert {lot.strategy_id for lot in pending} == {"s1", "s2"}


def test_ownership_ledger_distinguishes_multiple_lots_same_strategy():
    ledger = OptionOwnershipLedger()
    ts_us = int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000)
    ledger.record_intent(
        KernelIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            symbol="OPT:QQQ:20260326:590:P",
            target_qty=4.0,
            ts_bar_end_us=ts_us,
            order_ref="oref1",
            position_lot_id="lot:s1:1",
        )
    )
    ledger.record_intent(
        KernelIntentRecord(
            strategy_id="s1",
            decision_id="d2",
            symbol="OPT:QQQ:20260326:590:P",
            target_qty=2.0,
            ts_bar_end_us=ts_us + 60_000_000,
            order_ref="oref2",
            position_lot_id="lot:s1:2",
        )
    )

    pending = ledger.pending_entry_lots(strategy_id="s1", symbol="OPT:QQQ:20260326:590:P")
    assert len(pending) == 2
    assert {lot.position_lot_id for lot in pending} == {"lot:s1:1", "lot:s1:2"}


def test_ownership_ledger_promotes_pending_fill_to_live_only_after_mark_entry_live():
    ledger = OptionOwnershipLedger()
    ts_us = int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000)
    ledger.record_intent(
        KernelIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            symbol="OPT:QQQ:20260326:590:P",
            target_qty=4.0,
            ts_bar_end_us=ts_us,
            order_ref="oref1",
            position_lot_id="lot:s1:1",
            payload={"side": "PUT", "initial_qty": 4.0, "entry_mid": 1.2},
        )
    )
    ledger.resolve_entry_intent(position_lot_id="lot:s1:1", accepted=True)

    assert len(ledger.active_lots(strategy_id="s1")) == 0
    assert len(ledger.pending_entry_lots(strategy_id="s1")) == 1

    ledger.mark_entry_live(position_lot_id="lot:s1:1", filled_qty=4.0, payload={"avg_entry_px": 1.25})

    active = ledger.active_lots(strategy_id="s1")
    assert len(active) == 1
    assert active[0].status == "live"
    assert active[0].payload["avg_entry_px"] == 1.25


def test_ownership_ledger_exit_stays_pending_until_portfolio_reconcile():
    ledger = OptionOwnershipLedger()
    ts_us = int(datetime(2026, 3, 25, 11, 11, 0).timestamp() * 1_000_000)
    ledger.record_intent(
        KernelIntentRecord(
            strategy_id="s1",
            decision_id="d1",
            symbol="OPT:QQQ:20260326:590:P",
            target_qty=2.0,
            ts_bar_end_us=ts_us,
            order_ref="oref1",
            position_lot_id="lot:s1:1",
            payload={"side": "PUT", "initial_qty": 2.0, "entry_mid": 1.2},
        )
    )
    ledger.resolve_entry_intent(position_lot_id="lot:s1:1", accepted=True)
    ledger.mark_entry_live(position_lot_id="lot:s1:1", filled_qty=2.0)
    ledger.record_intent(
        KernelIntentRecord(
            strategy_id="s1",
            decision_id="x1",
            symbol="OPT:QQQ:20260326:590:P",
            target_qty=-2.0,
            ts_bar_end_us=ts_us + 60_000_000,
            order_ref="xoref1",
            position_lot_id="lot:s1:1",
        )
    )
    ledger.resolve_exit_intent(position_lot_id="lot:s1:1", accepted=True, exit_qty=2.0)

    active = ledger.active_lots(strategy_id="s1")
    assert len(active) == 1
    assert active[0].status == "pending_exit"
    assert active[0].qty == 2.0

    ledger.reconcile_symbol_live_qty(
        strategy_id="s1",
        symbol="OPT:QQQ:20260326:590:P",
        live_qty=0.0,
        payload={"sync_source": "ctx_portfolio"},
    )
    assert ledger.active_lots(strategy_id="s1") == []
