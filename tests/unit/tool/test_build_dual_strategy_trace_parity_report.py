from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_dual_strategy_trace_parity_report import build_dual_strategy_trace_parity_report


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_build_dual_strategy_trace_parity_report_passes_matching_streams(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "summary": {
                "qqq_is_traded": True,
                "index_entry_enabled": True,
                "index_final_state": "blocked",
            },
        },
    )
    _write_jsonl(
        audit_root / "2026-03-24" / "exec-10.jsonl",
        [
            {"ts": "2026-03-24T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_intent_submitted", "payload": {"side": "LONG", "qty": 8.0, "order_ref": "qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:1"}},
            {"ts": "2026-03-24T10:15:01-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_confirmed", "payload": {"side": "LONG", "qty": 8.0, "order_ref": "qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:1"}},
            {"ts": "2026-03-24T10:14:59-04:00", "strategy_id": "index_super_win_001", "symbol": "TQQQ", "trace_id": "trace-replica", "stage": "entry_intent_submitted", "payload": {"side": "LONG", "qty": 8.0, "order_ref": "iswv2:index_super_win_001:TQQQ:ENTRY:TRG:1"}},
            {"ts": "2026-03-24T10:15:02-04:00", "strategy_id": "index_super_win_001", "symbol": "TQQQ", "trace_id": "trace-replica", "stage": "entry_confirmed", "payload": {"side": "LONG", "qty": 8.0, "order_ref": "iswv2:index_super_win_001:TQQQ:ENTRY:TRG:1"}},
        ],
    )

    report = build_dual_strategy_trace_parity_report(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_001",
        qty_tolerance=1.0,
    )

    assert report["sample_count"] == 1
    assert report["pass_count"] == 1
    assert report["fail_count"] == 0
    assert report["missing_count"] == 0
    assert report["ready_for_parity"] is True
    assert report["rows"][0]["status"] == "pass"
    assert Path(report["rows"][0]["comparison_path"]).exists()


def test_build_dual_strategy_trace_parity_report_flags_mismatch(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "summary": {
                "qqq_is_traded": True,
                "index_entry_enabled": True,
                "index_final_state": "blocked",
            },
        },
    )
    _write_jsonl(
        audit_root / "2026-03-24" / "exec-10.jsonl",
        [
            {"ts": "2026-03-24T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_intent_submitted", "payload": {"side": "LONG", "qty": 8.0, "order_ref": "qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:1"}},
            {"ts": "2026-03-24T10:14:59-04:00", "strategy_id": "index_super_win_001", "symbol": "TQQQ", "trace_id": "trace-replica", "stage": "entry_intent_submitted", "payload": {"side": "SHORT", "qty": 8.0, "order_ref": "iswv2:index_super_win_001:TQQQ:ENTRY:TRG:1"}},
        ],
    )

    report = build_dual_strategy_trace_parity_report(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_001",
        qty_tolerance=1.0,
    )

    assert report["sample_count"] == 1
    assert report["pass_count"] == 0
    assert report["fail_count"] == 1
    assert report["ready_for_parity"] is False
    assert report["rows"][0]["status"] == "fail"
    comparison = json.loads(Path(report["rows"][0]["comparison_path"]).read_text(encoding="utf-8"))
    assert comparison["comparison"]["mismatch_count"] >= 1


def test_build_dual_strategy_trace_parity_report_marks_missing_trace(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "summary": {
                "qqq_is_traded": True,
                "index_entry_enabled": True,
                "index_final_state": "blocked",
            },
        },
    )
    _write_jsonl(
        audit_root / "2026-03-24" / "exec-10.jsonl",
        [
            {"ts": "2026-03-24T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_intent_submitted", "payload": {"side": "LONG", "qty": 8.0, "order_ref": "qqqv1:qqq_intraday_001:TQQQ:ENTRY:TRG:1"}},
        ],
    )

    report = build_dual_strategy_trace_parity_report(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_001",
        qty_tolerance=1.0,
    )

    assert report["sample_count"] == 1
    assert report["missing_count"] == 1
    assert report["ready_for_parity"] is False
    assert report["rows"][0]["status"] == "missing_trace"


def test_build_dual_strategy_trace_parity_report_skips_sample_when_no_trace_expected(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "QQQ_20260324T163500_000001.json",
        {
            "symbol": "QQQ",
            "captured_at_et": "2026-03-24T16:35:00-04:00",
            "summary": {
                "qqq_is_traded": False,
                "index_entry_enabled": False,
                "index_final_state": None,
            },
        },
    )

    report = build_dual_strategy_trace_parity_report(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_001",
        qty_tolerance=1.0,
    )

    assert report["sample_count"] == 1
    assert report["missing_count"] == 0
    assert report["fail_count"] == 0
    assert report["rows"][0]["status"] == "skipped_no_trace_expected"
