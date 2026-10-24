from services.strategy_runtime_v2.order_identity import (
    ORDER_NAMESPACE,
    OrderKind,
    build_order_ref,
    normalize_trigger_id,
    order_ref_belongs_to,
    parse_order_ref,
)


def test_normalize_trigger_id_keeps_last_16_alnum_chars():
    trigger = normalize_trigger_id("abc-123-XYZ-9876543210____")
    assert trigger == "123xyz9876543210"


def test_build_and_parse_order_ref_round_trip():
    order_ref = build_order_ref(
        strategy_id="index_super_win_001",
        symbol="aapl",
        kind=OrderKind.ENTRY,
        trigger_id="trace:ABC-123-xyz",
        suffix=("leg1", "001"),
    )

    parsed = parse_order_ref(order_ref)
    assert parsed is not None
    assert parsed.namespace == ORDER_NAMESPACE
    assert parsed.strategy_id == "index_super_win_001"
    assert parsed.symbol == "AAPL"
    assert parsed.kind == "ENTRY"
    assert parsed.trigger_id == "traceabc123xyz"
    assert parsed.suffix == ("leg1", "001")


def test_order_ref_belongs_to_respects_namespace_strategy_and_symbol():
    order_ref = build_order_ref(
        strategy_id="index_super_win_001",
        symbol="MSFT",
        kind="STOP",
        trigger_id="abc-001",
    )

    assert order_ref_belongs_to(
        order_ref,
        strategy_id="index_super_win_001",
        symbol="MSFT",
    )
    assert not order_ref_belongs_to(
        order_ref,
        strategy_id="qqq_intraday_001",
        symbol="MSFT",
    )
