import json
from pathlib import Path

from tool.query_index_super_win_audit_chain import build_index_audit_chain


def test_build_index_audit_chain_merges_rows_with_latest_snapshot(tmp_path: Path):
    audit_root = tmp_path / "audit"
    day_dir = audit_root / "2026-03-14"
    day_dir.mkdir(parents=True)
    (day_dir / "strategy-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-03-14T10:00:00-04:00",
                        "domain": "strategy",
                        "component": "index_super_win_strategy",
                        "stage": "entry_gate_blocked",
                        "trace_id": "trace-1",
                        "symbol": "AAPL",
                        "status": "blocked",
                        "reason": "signal_inactive",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-03-14T10:00:01-04:00",
                        "domain": "recon",
                        "component": "index_super_win_strategy",
                        "stage": "broker_open_orders_quarantined",
                        "trace_id": "trace-1",
                        "symbol": "AAPL",
                        "status": "quarantined",
                        "reason": "pre_entry:unknown_open_orders",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "index_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "trader_view_by_symbol": {
                    "AAPL": {
                        "final_state": "blocked",
                        "which_gate_blocked": "signal_inactive",
                    }
                },
                "chain_report_by_symbol": {"AAPL": {"entry_gate": {"status": "blocked", "reason": "signal_inactive"}}},
                "startup_consistency_by_symbol": {"AAPL": {"entry_enabled": True}},
                "runtime_health_by_symbol": {"AAPL": {"runtime_lifecycle": "flat"}},
                "conflict_report_by_symbol": {"AAPL": {"foreign_open_order_conflicts": 1}},
                "last_preview_by_symbol": {"AAPL": {"status": "QUARANTINED"}},
            }
        ),
        encoding="utf-8",
    )

    payload = build_index_audit_chain(
        audit_root=audit_root,
        snapshot_path=snapshot_path,
        strategy_id="index_super_win_001",
        date="2026-03-14",
        hour="10",
        trace_id="trace-1",
    )

    assert payload["summary"]["record_count"] == 2
    assert payload["query"]["symbol"] == "AAPL"
    assert payload["latest_symbol_state"]["trader_view"]["final_state"] == "blocked"
    assert payload["latest_symbol_state"]["conflict_report"]["foreign_open_order_conflicts"] == 1
    assert [row["stage"] for row in payload["rows"]] == ["entry_gate_blocked", "broker_open_orders_quarantined"]
