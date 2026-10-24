import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tool.ops import runtime_reconciliation_health as health


def test_runtime_reconciliation_health_blocks_on_active_quarantine(tmp_path, monkeypatch):
    audit_dir = tmp_path / "2026-05-02"
    audit_dir.mkdir(parents=True)
    row = {
        "ts": "2026-05-02T10:00:00-04:00",
        "stage": "position_audit_snapshot",
        "payload": {
            "db_open_count": 0,
            "ib_open_count": 0,
            "ghosts": 0,
            "orphans_unresolved": 0,
            "mismatches": 0,
            "position_mismatches": 0,
            "active_quarantine_count": 1,
            "position_reconciler": {"ib_positions": {}, "ledger_positions": {}, "mismatches": []},
        },
    }
    (audit_dir / "recon-test.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        health,
        "_query_latest_account_snapshot",
        lambda _now: {"ok": True, "blockers": []},
    )

    result = health.build_runtime_reconciliation_health(
        audit_root=tmp_path,
        day="20260502",
        now_et=datetime(2026, 5, 2, 10, 1, tzinfo=ZoneInfo("America/New_York")),
    )

    assert result["ok"] is False
    assert "runtime_active_quarantine_present" in result["blockers"]
    assert result["latest_position_audit_payload"]["active_quarantine_count"] == 1
