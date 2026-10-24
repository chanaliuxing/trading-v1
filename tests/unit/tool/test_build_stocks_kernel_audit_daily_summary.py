from datetime import datetime

from strategies.stocks_kernel.runtime.audit import LocalAuditWriter
from strategies.stocks_kernel.types import KernelExecutionEvent
from tool.report.build_stocks_kernel_audit_daily_summary import build_daily_summary


def test_build_daily_summary_counts_domain_and_stage(tmp_path):
    writer = LocalAuditWriter(root_dir=tmp_path)
    writer.write_event(
        domain="strategy",
        event=KernelExecutionEvent(stage="gate_blocked", strategy_id="alpha", symbol="QQQ", reason="snapshot_stale"),
        now=datetime(2026, 3, 30, 10, 0, 0),
    )
    writer.write_event(
        domain="market",
        event=KernelExecutionEvent(stage="market_summary", strategy_id="alpha", symbol="QQQ", reason="hold"),
        now=datetime(2026, 3, 30, 10, 1, 0),
    )

    payload = build_daily_summary(audit_root=tmp_path, date="2026-03-30")

    assert payload["row_count"] == 2
    assert payload["by_domain"]["market"] == 1
    assert payload["by_domain"]["strategy"] == 1
