import json
from datetime import datetime

from strategies.stocks_kernel.runtime.audit import LocalAuditWriter
from strategies.stocks_kernel.types import KernelExecutionEvent
from tool.report.build_stocks_kernel_validation_daily_bundle import build_validation_daily_bundle


def test_build_validation_daily_bundle_merges_reports(tmp_path):
    writer = LocalAuditWriter(root_dir=tmp_path / "audit")
    writer.write_event(
        domain="strategy",
        event=KernelExecutionEvent(stage="gate_blocked", strategy_id="alpha", symbol="QQQ", reason="snapshot_stale"),
        now=datetime(2026, 3, 30, 10, 0, 0),
    )
    parity = tmp_path / "parity.json"
    shadow = tmp_path / "shadow.json"
    paper = tmp_path / "paper.json"
    parity.write_text(json.dumps({"match_ratio": 0.9}), encoding="utf-8")
    shadow.write_text(json.dumps({"match_ratio": 0.8}), encoding="utf-8")
    paper.write_text(json.dumps({"status": "ok"}), encoding="utf-8")

    payload = build_validation_daily_bundle(
        audit_root=tmp_path / "audit",
        date="2026-03-30",
        parity_path=parity,
        shadow_path=shadow,
        paper_path=paper,
    )

    assert payload["audit_summary"]["row_count"] == 1
    assert payload["parity_report"]["match_ratio"] == 0.9
    assert payload["shadow_report"]["match_ratio"] == 0.8
    assert payload["paper_report"]["status"] == "ok"
