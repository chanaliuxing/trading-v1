from strategies.stocks_kernel.runtime.bridge_events import (
    build_broker_submit_ack_event,
    build_db_persist_event,
    build_fill_received_event,
)


def test_bridge_event_builders_fill_expected_fields():
    ack = build_broker_submit_ack_event(
        strategy_id="alpha",
        strategy_run_id="run-1",
        symbol="qqq",
        decision_id="d1",
        trace_id="t1",
        order_ref="stkv1:alpha:QQQ:ENTRY:LOT:1",
        broker_order_id="1001",
        perm_id=55,
        payload={"foo": "bar"},
    )
    fill = build_fill_received_event(
        strategy_id="alpha",
        strategy_run_id="run-1",
        symbol="qqq",
        trace_id="t1",
        order_ref="stkv1:alpha:QQQ:ENTRY:LOT:1",
        broker_order_id="1001",
        perm_id=55,
        qty=100,
        price=500.5,
        exec_id="e-1",
    )
    db = build_db_persist_event(
        strategy_id="alpha",
        strategy_run_id="run-1",
        symbol="qqq",
        trace_id="t1",
        decision_id="d1",
        order_ref="stkv1:alpha:QQQ:ENTRY:LOT:1",
        success=True,
        payload={"row_id": "abc"},
    )

    assert ack.stage == "broker_submit_ack"
    assert ack.domain == "exec"
    assert fill.stage == "fill_received"
    assert fill.payload["exec_id"] == "e-1"
    assert db.stage == "db_persisted"
    assert db.status == "persisted"
