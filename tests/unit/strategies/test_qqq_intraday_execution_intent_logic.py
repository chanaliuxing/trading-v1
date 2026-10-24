from strategies.qqq_intraday_execution_intent_logic import build_close_only_extras, exit_target_qty


def test_exit_target_qty_uses_signed_direction() -> None:
    assert exit_target_qty(qty_signed=5.0) == -5.0
    assert exit_target_qty(qty_signed=-5.0) == 5.0
    assert exit_target_qty(qty_signed=5.0, qty_to_exit=2) == -2.0
    assert exit_target_qty(qty_signed=-5.0, qty_to_exit=2) == 2.0


def test_build_close_only_extras_contains_common_fields() -> None:
    extras = build_close_only_extras(
        reason="tp1_hit",
        trigger_id="trg1",
        trace_id="d1",
        order_ref="iswv2:xx:TQQQ:TP1:TRG:trg1",
        position_qty_hint=8.0,
        extra_flags={"tp1_partial_exit": True},
    )
    assert extras["reason"] == "tp1_hit"
    assert extras["trigger_id"] == "trg1"
    assert extras["trace_id"] == "d1"
    assert extras["order_ref"].endswith(":TRG:trg1")
    assert extras["close_only"] is True
    assert extras["tp1_partial_exit"] is True
    assert float(extras["position_qty_hint"]) == 8.0
