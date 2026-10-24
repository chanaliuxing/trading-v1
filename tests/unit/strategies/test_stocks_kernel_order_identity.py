from strategies.stocks_kernel.execution.order_identity import (
    build_order_ref,
    order_ref_belongs_to_strategy,
    parse_order_ref,
)


def test_order_ref_builder_sanitizes_colons_in_trigger_id():
    order_ref = build_order_ref(
        namespace="stkv1",
        strategy_id="alpha",
        symbol="SOXL",
        kind="ENTRY",
        trigger_id="alpha:SOXL:123",
        lot_id="lot:1",
    )

    parsed = parse_order_ref(order_ref)
    assert parsed is not None
    assert parsed.namespace == "stkv1"
    assert parsed.strategy_id == "alpha"
    assert parsed.symbol == "SOXL"
    assert parsed.trigger_id == "alpha_SOXL_123"
    assert parsed.lot_id == "lot_1"
    assert order_ref_belongs_to_strategy(order_ref, namespace="stkv1", strategy_id="alpha") is True
