from strategies.stocks_kernel.modules.execution_quality import ExecutionQualityModule


def test_execution_quality_long_entry_slippage_positive_for_adverse_fill():
    module = ExecutionQualityModule()
    module.register_intent(
        order_ref="stkv1:alpha:SOXL:ENTRY:lot-1",
        symbol="SOXL",
        side="LONG",
        order_role="ENTRY",
        expected_price=50.0,
        trigger_price=50.0,
        now_ts_epoch_s=100.0,
    )
    module.register_submit_ack(order_ref="stkv1:alpha:SOXL:ENTRY:lot-1", now_ts_epoch_s=101.0)

    payload = module.register_fill(
        order_ref="stkv1:alpha:SOXL:ENTRY:lot-1",
        symbol="SOXL",
        order_role="ENTRY",
        side="LONG",
        fill_price=50.1,
        fill_qty=100.0,
        now_ts_epoch_s=102.5,
    )

    assert payload["slippage_bps"] is not None
    assert payload["slippage_bps"] > 0.0
    assert payload["submit_to_fill_latency_ms"] == 1500.0


def test_execution_quality_exit_reverses_slippage_sign():
    module = ExecutionQualityModule()
    module.register_intent(
        order_ref="stkv1:alpha:SOXL:FLATTEN:lot-1",
        symbol="SOXL",
        side="LONG",
        order_role="FLATTEN",
        expected_price=50.0,
        trigger_price=50.0,
        now_ts_epoch_s=200.0,
    )
    module.register_submit_ack(order_ref="stkv1:alpha:SOXL:FLATTEN:lot-1", now_ts_epoch_s=200.5)

    payload = module.register_fill(
        order_ref="stkv1:alpha:SOXL:FLATTEN:lot-1",
        symbol="SOXL",
        order_role="FLATTEN",
        side="LONG",
        fill_price=49.8,
        fill_qty=100.0,
        now_ts_epoch_s=201.0,
    )

    assert payload["slippage_bps"] is not None
    assert payload["slippage_bps"] > 0.0
