from types import SimpleNamespace

from strategies.qqq_intraday_submit_preview_logic import preview_row, preview_row_from_outcome


def test_preview_row_basic() -> None:
    row = preview_row(
        status="FLATTEN_PREVIEW",
        reason="logic_retreat",
        exec_symbol="TQQQ",
        order_ref="oref-1",
        extra={"qty": 5},
    )
    assert row["status"] == "FLATTEN_PREVIEW"
    assert row["reason"] == "logic_retreat"
    assert row["exec_symbol"] == "TQQQ"
    assert row["order_ref"] == "oref-1"
    assert row["qty"] == 5


def test_preview_row_from_outcome() -> None:
    outcome = SimpleNamespace(preview_status="FLATTEN_SUBMITTED", reason="logic_retreat")
    row = preview_row_from_outcome(
        outcome=outcome,
        exec_symbol="TQQQ",
        order_ref="oref-2",
        extra={"qty": 8, "live_qty": 10},
    )
    assert row["status"] == "FLATTEN_SUBMITTED"
    assert row["reason"] == "logic_retreat"
    assert row["exec_symbol"] == "TQQQ"
    assert row["order_ref"] == "oref-2"
    assert row["qty"] == 8
    assert row["live_qty"] == 10
