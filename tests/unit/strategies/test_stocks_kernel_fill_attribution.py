from strategies.stocks_kernel.execution.fill_attribution import FillAttributionEngine
from strategies.stocks_kernel.execution.order_identity import build_order_ref
from strategies.stocks_kernel.execution.lot_ledger import PositionLotLedger
from strategies.stocks_kernel.types import KernelFillReport, PositionLot, PositionLotStatus


def _lot(
    *,
    lot_id: str,
    strategy_id: str,
    symbol: str,
    side: str = "LONG",
    order_ref: str,
    broker_order_id: str | None = None,
    perm_id: int | None = None,
) -> PositionLot:
    return PositionLot(
        position_lot_id=lot_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        opened_by_decision_id=f"decision-{lot_id}",
        opened_by_trigger_id=f"trigger-{lot_id}",
        entry_order_ref=order_ref,
        entry_order_id=broker_order_id,
        entry_perm_id=perm_id,
        live_qty=100.0,
        filled_qty=100.0,
        avg_entry_price=100.0,
        state=PositionLotStatus.LIVE,
    )


def test_fill_attribution_prefers_perm_id_for_same_symbol_multi_strategy():
    ledger = PositionLotLedger()
    ledger.open_lot(
        _lot(
            lot_id="lot-a",
            strategy_id="alpha",
            symbol="QQQ",
            order_ref=build_order_ref(namespace="stkv1", strategy_id="alpha", symbol="QQQ", kind="ENTRY", lot_id="lot-a"),
            perm_id=101,
        )
    )
    ledger.open_lot(
        _lot(
            lot_id="lot-b",
            strategy_id="beta",
            symbol="QQQ",
            order_ref=build_order_ref(namespace="stkv1", strategy_id="beta", symbol="QQQ", kind="ENTRY", lot_id="lot-b"),
            perm_id=202,
        )
    )
    engine = FillAttributionEngine(ledger)

    result = engine.attribute_fill(
        KernelFillReport(symbol="QQQ", perm_id=202, qty=100, price=501.2, exec_id="e-1")
    )

    assert result.position_lot_id == "lot-b"
    assert result.strategy_id == "beta"
    assert result.reason == "matched_by_broker_identity"
    assert result.quarantine_required is False


def test_fill_attribution_uses_order_ref_lot_id_when_broker_ids_missing():
    ledger = PositionLotLedger()
    order_ref = build_order_ref(
        namespace="stkv1",
        strategy_id="alpha",
        symbol="QQQ",
        kind="ENTRY",
        trigger_id="t-1",
        lot_id="lot-a",
    )
    ledger.open_lot(_lot(lot_id="lot-a", strategy_id="alpha", symbol="QQQ", order_ref=order_ref))
    engine = FillAttributionEngine(ledger)

    result = engine.attribute_fill(
        KernelFillReport(symbol="QQQ", order_ref=order_ref, qty=100, price=501.2, exec_id="e-2")
    )

    assert result.position_lot_id == "lot-a"
    assert result.strategy_id == "alpha"
    assert result.reason == "matched_by_order_ref_lot_id"
    assert result.quarantine_required is False


def test_fill_attribution_quarantines_ambiguous_strategy_symbol_match():
    ledger = PositionLotLedger()
    strategy_id = "alpha"
    ledger.open_lot(
        _lot(
            lot_id="lot-a",
            strategy_id=strategy_id,
            symbol="QQQ",
            order_ref=build_order_ref(namespace="stkv1", strategy_id=strategy_id, symbol="QQQ", kind="ENTRY", lot_id="lot-a"),
        )
    )
    ledger.open_lot(
        _lot(
            lot_id="lot-b",
            strategy_id=strategy_id,
            symbol="QQQ",
            order_ref=build_order_ref(namespace="stkv1", strategy_id=strategy_id, symbol="QQQ", kind="ENTRY", lot_id="lot-b"),
        )
    )
    engine = FillAttributionEngine(ledger)
    order_ref = build_order_ref(namespace="stkv1", strategy_id=strategy_id, symbol="QQQ", kind="ENTRY")

    result = engine.attribute_fill(
        KernelFillReport(symbol="QQQ", order_ref=order_ref, qty=100, price=501.2, exec_id="e-3")
    )

    assert result.position_lot_id is None
    assert result.strategy_id is None
    assert result.reason == "fill_order_ref_strategy_symbol_ambiguous"
    assert result.quarantine_required is True


def test_fill_attribution_quarantines_unknown_fill_without_identity():
    ledger = PositionLotLedger()
    engine = FillAttributionEngine(ledger)

    result = engine.attribute_fill(
        KernelFillReport(symbol="QQQ", qty=100, price=501.2, exec_id="e-4")
    )

    assert result.position_lot_id is None
    assert result.reason == "fill_unattributed"
    assert result.quarantine_required is True
