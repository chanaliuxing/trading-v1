from datetime import datetime

from strategies.stocks_kernel.runtime.audit import AsyncLocalAuditWriter, query_audit_chain
from strategies.stocks_kernel.types import KernelExecutionEvent


def test_async_local_audit_writer_flushes_events(tmp_path):
    writer = AsyncLocalAuditWriter(root_dir=tmp_path, max_queue_size=8)
    writer.write_event(
        domain="exec",
        event=KernelExecutionEvent(
            stage="entry_lot_opened",
            strategy_id="alpha",
            symbol="QQQ",
            trace_id="trace-1",
        ),
        now=datetime(2026, 3, 30, 10, 0, 0),
    )
    writer.flush(timeout_sec=1.0)
    rows = query_audit_chain(root_dir=tmp_path, trace_id="trace-1")
    writer.close()

    assert len(rows) == 1
    assert writer.enqueue_failures == 0
    assert writer.write_failures == 0


def test_async_local_audit_writer_fail_open_when_queue_full(tmp_path):
    writer = AsyncLocalAuditWriter(root_dir=tmp_path, max_queue_size=1)
    writer.close()
    writer.write_event(
        domain="exec",
        event=KernelExecutionEvent(
            stage="entry_lot_opened",
            strategy_id="alpha",
            symbol="QQQ",
        ),
        now=datetime(2026, 3, 30, 10, 0, 0),
    )

    assert writer.enqueue_failures >= 1
