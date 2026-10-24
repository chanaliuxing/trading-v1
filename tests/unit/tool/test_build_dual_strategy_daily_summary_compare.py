from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_dual_strategy_daily_summary_compare import build_dual_strategy_daily_summary_compare



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def test_build_dual_strategy_daily_summary_compare_green(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    qqq_dir = tmp_path / "qqq"
    replica_dir = tmp_path / "replica"
    out_dir = tmp_path / "out"

    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "summary": {"index_gate_reason": "ownership_conflict"},
        },
    )
    _write_json(
        qqq_dir / "TQQQ_20260324T101500_000001.json",
        {
            "events": [
                {"ts": "2026-03-24T10:14:58-04:00", "stage": "entry_intent_submitted", "reason": "qqq_intraday_entry"},
                {"ts": "2026-03-24T10:15:01-04:00", "stage": "flatten_submitted", "reason": "logic_retreat"},
                {"ts": "2026-03-24T10:15:02-04:00", "stage": "protective_stop_submitted", "reason": "audit_self_heal"},
            ]
        },
    )
    _write_json(
        replica_dir / "TQQQ_20260324T101500_000001.json",
        {
            "events": [
                {"ts": "2026-03-24T10:15:00-04:00", "stage": "entry_gate_blocked", "reason": "ownership_conflict", "payload": {"reason": "ownership_conflict"}},
                {"ts": "2026-03-24T10:14:59-04:00", "stage": "entry_intent_submitted", "reason": "qqq_intraday_entry"},
                {"ts": "2026-03-24T10:15:03-04:00", "stage": "flatten_submitted", "reason": "logic_retreat"},
                {"ts": "2026-03-24T10:15:04-04:00", "stage": "protection_audit_self_heal", "reason": "audit_self_heal"},
            ]
        },
    )

    report = build_dual_strategy_daily_summary_compare(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_dir,
        replica_trace_dir=replica_dir,
        output_dir=out_dir,
        submit_timing_tolerance_sec=5.0,
    )

    assert report["sample_count"] == 1
    assert report["summary_compare_green"] is True
    assert report["gate_reason"]["mismatch"] == 0
    assert report["submit_timing"]["mismatch"] == 0
    assert report["flatten_reason"]["mismatch"] == 0
    assert report["stop_rebuild"]["mismatch"] == 0
    assert Path(report["report_path"]).exists()



def test_build_dual_strategy_daily_summary_compare_flags_mismatches(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    qqq_dir = tmp_path / "qqq"
    replica_dir = tmp_path / "replica"
    out_dir = tmp_path / "out"

    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "summary": {"index_gate_reason": "ownership_conflict"},
        },
    )
    _write_json(
        qqq_dir / "TQQQ_20260324T101500_000001.json",
        {
            "events": [
                {"ts": "2026-03-24T10:14:58-04:00", "stage": "entry_intent_submitted", "reason": "qqq_intraday_entry"},
                {"ts": "2026-03-24T10:15:01-04:00", "stage": "flatten_submitted", "reason": "tp1_then_eod"},
            ]
        },
    )
    _write_json(
        replica_dir / "TQQQ_20260324T101500_000001.json",
        {
            "events": [
                {"ts": "2026-03-24T10:15:30-04:00", "stage": "entry_gate_blocked", "reason": "unexpected_block", "payload": {"reason": "unexpected_block"}},
                {"ts": "2026-03-24T10:15:45-04:00", "stage": "entry_intent_submitted", "reason": "qqq_intraday_entry"},
                {"ts": "2026-03-24T10:16:00-04:00", "stage": "flatten_submitted", "reason": "logic_retreat"},
                {"ts": "2026-03-24T10:16:01-04:00", "stage": "protection_audit_self_heal", "reason": "audit_self_heal"},
            ]
        },
    )

    report = build_dual_strategy_daily_summary_compare(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_dir,
        replica_trace_dir=replica_dir,
        output_dir=out_dir,
        submit_timing_tolerance_sec=5.0,
    )

    assert report["summary_compare_green"] is False
    assert report["gate_reason"]["mismatch"] == 1
    assert report["submit_timing"]["mismatch"] == 1
    assert report["flatten_reason"]["mismatch"] == 1
    assert report["stop_rebuild"]["mismatch"] == 1
