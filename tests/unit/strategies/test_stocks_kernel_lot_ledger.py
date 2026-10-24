from strategies.stocks_kernel.execution.lot_ledger import PositionLotLedger
from strategies.stocks_kernel.types import PositionLot, PositionLotStatus


def _lot(*, lot_id: str, strategy_id: str, symbol: str, side: str = "LONG") -> PositionLot:
    return PositionLot(
        position_lot_id=lot_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        opened_by_decision_id=f"decision-{lot_id}",
        opened_by_trigger_id=f"trigger-{lot_id}",
        entry_order_ref=f"stkv1:{strategy_id}:{symbol}:ENTRY:LOT:{lot_id}",
    )


def test_lot_ledger_tracks_same_symbol_across_multiple_strategies_without_collision():
    ledger = PositionLotLedger()
    ledger.open_lot(_lot(lot_id="lot-a", strategy_id="strategy_a", symbol="TQQQ", side="LONG"))
    ledger.open_lot(_lot(lot_id="lot-b", strategy_id="strategy_b", symbol="TQQQ", side="SHORT"))

    ledger.record_entry_fill(position_lot_id="lot-a", fill_qty=100, fill_price=40.0, broker_order_id="1", perm_id=101)
    ledger.record_entry_fill(position_lot_id="lot-b", fill_qty=80, fill_price=88.0, broker_order_id="2", perm_id=202)

    view_a = ledger.symbol_view(strategy_id="strategy_a", symbol="TQQQ")
    view_b = ledger.symbol_view(strategy_id="strategy_b", symbol="TQQQ")

    assert view_a.long_qty == 100
    assert view_a.short_qty == 0
    assert view_a.net_qty == 100
    assert view_b.long_qty == 0
    assert view_b.short_qty == 80
    assert view_b.net_qty == -80


def test_lot_ledger_flattens_only_target_lot():
    ledger = PositionLotLedger()
    ledger.open_lot(_lot(lot_id="lot-a", strategy_id="strategy_a", symbol="AAPL", side="LONG"))
    ledger.open_lot(_lot(lot_id="lot-b", strategy_id="strategy_b", symbol="AAPL", side="LONG"))
    ledger.record_entry_fill(position_lot_id="lot-a", fill_qty=50, fill_price=100.0)
    ledger.record_entry_fill(position_lot_id="lot-b", fill_qty=70, fill_price=101.0)

    ledger.request_exit(position_lot_id="lot-a", flatten_order_ref="stkv1:strategy_a:AAPL:FLATTEN:LOT:lot-a")
    ledger.record_exit_fill(position_lot_id="lot-a", fill_qty=50)

    lot_a = ledger.get_lot("lot-a")
    lot_b = ledger.get_lot("lot-b")

    assert lot_a is not None and lot_a.state == PositionLotStatus.CLOSED
    assert lot_a.live_qty == 0
    assert lot_a.flatten_order_refs == []
    assert lot_b is not None and lot_b.state == PositionLotStatus.LIVE
    assert lot_b.live_qty == 70


def test_lot_ledger_partial_exit_fill_clears_inflight_flatten_refs_and_keeps_lot_live():
    ledger = PositionLotLedger()
    ledger.open_lot(_lot(lot_id="lot-p1", strategy_id="strategy_a", symbol="AAPL", side="LONG"))
    ledger.record_entry_fill(position_lot_id="lot-p1", fill_qty=10, fill_price=100.0)
    ledger.request_exit(position_lot_id="lot-p1", flatten_order_ref="stkv1:strategy_a:AAPL:FLATTEN:LOT:lot-p1")

    lot_before = ledger.get_lot("lot-p1")
    assert lot_before is not None
    assert lot_before.flatten_order_refs

    ledger.record_exit_fill(position_lot_id="lot-p1", fill_qty=4)

    lot_after = ledger.get_lot("lot-p1")
    assert lot_after is not None
    assert lot_after.state == PositionLotStatus.LIVE
    assert lot_after.live_qty == 6
    assert lot_after.flatten_order_refs == []


def test_symbol_view_is_derived_from_lots_not_independent_state():
    ledger = PositionLotLedger()
    ledger.open_lot(_lot(lot_id="lot-1", strategy_id="strategy_a", symbol="MSFT", side="LONG"))
    ledger.open_lot(_lot(lot_id="lot-2", strategy_id="strategy_a", symbol="MSFT", side="LONG"))
    ledger.record_entry_fill(position_lot_id="lot-1", fill_qty=10, fill_price=200.0)
    ledger.record_entry_fill(position_lot_id="lot-2", fill_qty=15, fill_price=201.0)

    view = ledger.symbol_view(strategy_id="strategy_a", symbol="MSFT")

    assert view.open_lot_count == 2
    assert view.long_qty == 25
    assert view.short_qty == 0
    assert view.net_qty == 25


def test_quarantine_marks_only_target_lot():
    ledger = PositionLotLedger()
    ledger.open_lot(_lot(lot_id="lot-1", strategy_id="strategy_a", symbol="QQQ", side="LONG"))
    ledger.open_lot(_lot(lot_id="lot-2", strategy_id="strategy_a", symbol="QQQ", side="LONG"))
    ledger.record_entry_fill(position_lot_id="lot-1", fill_qty=10, fill_price=200.0)
    ledger.record_entry_fill(position_lot_id="lot-2", fill_qty=15, fill_price=201.0)

    ledger.quarantine_lot(position_lot_id="lot-1", reason="ambiguous_fill")

    lot_1 = ledger.get_lot("lot-1")
    lot_2 = ledger.get_lot("lot-2")

    assert lot_1 is not None and lot_1.state == PositionLotStatus.QUARANTINED
    assert lot_1.extras["quarantine_reason"] == "ambiguous_fill"
    assert lot_2 is not None and lot_2.state == PositionLotStatus.LIVE
