from __future__ import annotations

import json
from pathlib import Path

from tool.report.build_order_truth_runtime_evidence import build_order_truth_runtime_evidence


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_build_order_truth_runtime_evidence_passes_when_false_block_removed_and_legit_block_seen(tmp_path: Path) -> None:
    rows = [
        {
            "strategy_id": "qqq_intraday_001",
            "stage": "gate_blocked",
            "reason": "sign_mismatch",
            "payload": {},
        },
        {
            "strategy_id": "qqq_intraday_001",
            "stage": "position_sync_qty_resolution",
            "payload": {"qty_resolved": {"TQQQ": 0.0, "SQQQ": 0.0}},
        },
        {
            "strategy_id": "other_strategy",
            "stage": "entry_blocked_legacy_open_orders",
            "reason": "legacy_open_orders_block",
            "payload": {},
        },
    ]
    _write_jsonl(tmp_path / "2026-04-23" / "strategy-10.jsonl", rows)

    report = build_order_truth_runtime_evidence(audit_root=tmp_path, date="2026-04-23")

    assert report["ok"] is True
    assert report["recommendation"] == "PASS"
    assert report["false_block_removed"] is True
    assert report["legacy_open_orders_block_count"] == 0
    assert report["legitimate_block_observed"] is True
    assert report["legitimate_block_reason_counts"] == {"sign_mismatch": 1}
    assert report["position_sync_flat_count"] == 1


def test_build_order_truth_runtime_evidence_holds_when_legacy_block_still_present(tmp_path: Path) -> None:
    rows = [
        {
            "strategy_id": "qqq_intraday_001",
            "stage": "entry_blocked_legacy_open_orders",
            "reason": "legacy_open_orders_block",
            "payload": {},
        }
    ]
    _write_jsonl(tmp_path / "2026-04-23" / "strategy-10.jsonl", rows)

    report = build_order_truth_runtime_evidence(audit_root=tmp_path, date="2026-04-23")

    assert report["ok"] is False
    assert report["recommendation"] == "HOLD"
    assert report["false_block_removed"] is False
    assert report["legacy_open_orders_block_count"] == 1
    assert "legacy_open_orders_block_still_present" in report["reasons"]


def test_build_order_truth_runtime_evidence_continues_when_strategy_rows_exist_without_legit_block(tmp_path: Path) -> None:
    rows = [
        {
            "strategy_id": "qqq_intraday_001",
            "stage": "position_sync_qty_resolution",
            "payload": {"qty_resolved": {"TQQQ": 0.0, "SQQQ": 0.0}},
        }
    ]
    _write_jsonl(tmp_path / "2026-04-23" / "strategy-10.jsonl", rows)

    report = build_order_truth_runtime_evidence(audit_root=tmp_path, date="2026-04-23")

    assert report["ok"] is True
    assert report["recommendation"] == "CONTINUE"
    assert report["false_block_removed"] is True
    assert report["legitimate_block_observed"] is False
    assert "legitimate_runtime_block_not_observed_yet" in report["reasons"]


def test_build_order_truth_runtime_evidence_accepts_option_live_alias(tmp_path: Path) -> None:
    rows = [
        {
            "strategy_id": "qqq_intraday_option_v1_001",
            "stage": "position_sync_qty_resolution",
            "payload": {"qty_resolved": {"TQQQ": 0.0, "SQQQ": 0.0}},
        }
    ]
    _write_jsonl(tmp_path / "2026-04-23" / "strategy-10.jsonl", rows)

    report = build_order_truth_runtime_evidence(audit_root=tmp_path, date="2026-04-23")

    assert report["ok"] is True
    assert report["recommendation"] == "CONTINUE"
    assert report["strategy_ids_checked"] == ["qqq_intraday_001", "qqq_intraday_option_v1_001"]
    assert report["matched_strategy_ids"] == ["qqq_intraday_option_v1_001"]
