import pytest

from strategies.stocks_kernel.execution.order_binding_book import LotOrderBindingBook
from strategies.stocks_kernel.execution.order_identity import build_order_ref
from strategies.stocks_kernel.types import LotOrderBinding


def _binding(
    *,
    lot_id: str,
    strategy_id: str,
    symbol: str,
    role: str,
    broker_order_id: str | None = None,
    perm_id: int | None = None,
) -> LotOrderBinding:
    return LotOrderBinding(
        position_lot_id=lot_id,
        strategy_id=strategy_id,
        symbol=symbol,
        order_role=role,
        order_ref=build_order_ref(
            namespace="stkv1",
            strategy_id=strategy_id,
            symbol=symbol,
            kind=role,
            lot_id=lot_id,
        ),
        broker_order_id=broker_order_id,
        perm_id=perm_id,
    )


def test_binding_book_resolves_same_symbol_multi_strategy_without_collision():
    book = LotOrderBindingBook()
    entry_a = book.bind(_binding(lot_id="lot-a", strategy_id="alpha", symbol="QQQ", role="ENTRY", broker_order_id="1001", perm_id=101))
    entry_b = book.bind(_binding(lot_id="lot-b", strategy_id="beta", symbol="QQQ", role="ENTRY", broker_order_id="1002", perm_id=202))

    assert book.resolve(perm_id=101) == entry_a
    assert book.resolve(broker_order_id="1002") == entry_b
    assert book.resolve(order_ref=entry_a.order_ref) == entry_a


def test_binding_book_rejects_duplicate_broker_identity_across_lots():
    book = LotOrderBindingBook()
    book.bind(_binding(lot_id="lot-a", strategy_id="alpha", symbol="QQQ", role="ENTRY", broker_order_id="1001", perm_id=101))

    with pytest.raises(ValueError, match="broker_order_id already bound"):
        book.bind(_binding(lot_id="lot-b", strategy_id="beta", symbol="QQQ", role="ENTRY", broker_order_id="1001", perm_id=202))


def test_binding_book_builds_one_lot_one_protection_chain_view():
    book = LotOrderBindingBook()
    book.bind(_binding(lot_id="lot-a", strategy_id="alpha", symbol="QQQ", role="ENTRY", broker_order_id="1001", perm_id=101))
    stop = book.bind(_binding(lot_id="lot-a", strategy_id="alpha", symbol="QQQ", role="STOP", broker_order_id="1003", perm_id=303))
    flatten = book.bind(_binding(lot_id="lot-a", strategy_id="alpha", symbol="QQQ", role="FLATTEN", broker_order_id="1004", perm_id=404))
    book.bind(_binding(lot_id="lot-b", strategy_id="beta", symbol="QQQ", role="STOP", broker_order_id="2003", perm_id=505))

    chain = book.protection_chain(position_lot_id="lot-a")

    assert chain.position_lot_id == "lot-a"
    assert chain.strategy_id == "alpha"
    assert chain.symbol == "QQQ"
    assert chain.entry_order_ref is not None
    assert chain.stop_order_refs == (stop.order_ref,)
    assert chain.flatten_order_refs == (flatten.order_ref,)
