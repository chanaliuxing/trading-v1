from datetime import datetime
from pathlib import Path

from strategies.stocks_kernel.runtime.audit import LocalAuditWriter
from strategies.stocks_kernel.types import KernelExecutionEvent
from tool.query_stocks_kernel_audit_chain import build_stocks_kernel_audit_chain


def test_build_stocks_kernel_audit_chain(tmp_path: Path):
    writer = LocalAuditWriter(root_dir=tmp_path)
    writer.write_event(
        domain="exec",
        event=KernelExecutionEvent(
            stage="entry_lot_opened",
            strategy_id="alpha",
            symbol="QQQ",
            position_lot_id="lot-1",
            order_ref="stkv1:alpha:QQQ:ENTRY:LOT:lot-1",
        ),
        now=datetime(2026, 3, 30, 10, 0, 0),
    )

    payload = build_stocks_kernel_audit_chain(
        audit_root=tmp_path,
        strategy_id="alpha",
        symbol="QQQ",
        position_lot_id="lot-1",
        order_ref="stkv1:alpha:QQQ:ENTRY:LOT:lot-1",
        domain="exec",
        date="2026-03-30",
        hour="10",
    )

    assert payload["count"] == 1
    assert payload["query"]["symbol"] == "QQQ"
    assert payload["query"]["order_ref"] == "stkv1:alpha:QQQ:ENTRY:LOT:lot-1"
    assert payload["query"]["domain"] == "exec"
    assert payload["query"]["date"] == "2026-03-30"
    assert payload["query"]["hour"] == "10"
    assert payload["rows"][0]["stage"] == "entry_lot_opened"
