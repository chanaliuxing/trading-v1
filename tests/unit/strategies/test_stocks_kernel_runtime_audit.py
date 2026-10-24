from datetime import datetime

from strategies.stocks_kernel.runtime.audit import LocalAuditWriter, query_audit_chain
from strategies.stocks_kernel.types import KernelExecutionEvent


def test_local_audit_writer_and_query_chain(tmp_path):
    writer = LocalAuditWriter(root_dir=tmp_path)
    writer.write_event(
        domain="exec",
        event=KernelExecutionEvent(
            stage="entry_fill_applied",
            strategy_id="alpha",
            symbol="QQQ",
            position_lot_id="lot-1",
            order_ref="stkv1:alpha:QQQ:ENTRY:LOT:lot-1",
        ),
        now=datetime(2026, 3, 30, 10, 5, 0),
    )
    writer.write_event(
        domain="exec",
        event=KernelExecutionEvent(
            stage="exit_fill_applied",
            strategy_id="alpha",
            symbol="QQQ",
            position_lot_id="lot-1",
            order_ref="stkv1:alpha:QQQ:STOP:LOT:lot-1",
        ),
        now=datetime(2026, 3, 30, 10, 6, 0),
    )

    rows = query_audit_chain(root_dir=tmp_path, strategy_id="alpha", symbol="QQQ", position_lot_id="lot-1")

    assert len(rows) == 2
    assert rows[0]["stage"] == "entry_fill_applied"
    assert rows[1]["stage"] == "exit_fill_applied"
    assert rows[0]["domain"] == "exec"
