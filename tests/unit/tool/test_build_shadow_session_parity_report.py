from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_shadow_session_parity_report import build_shadow_session_parity_report



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def test_build_shadow_session_parity_report_passes_matching_trace(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    actual_dir = tmp_path / "actual"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "AAPL_20260323T103000_000001.json",
        {
            "symbol": "AAPL",
            "captured_at_et": "2026-03-23T10:30:00-04:00",
            "summary": {
                "qqq_is_traded": True,
                "qqq_exec_symbol": "AAPL",
                "index_final_state": "blocked",
                "index_gate_reason": "ownership_conflict",
            },
        },
    )
    _write_json(
        actual_dir / "AAPL_20260323T103000_000001.json",
        {
            "events": [
                {
                    "stage": "entry_gate_blocked",
                    "symbol": "AAPL",
                    "trace_id": "shadow:AAPL:AAPL_20260323T103000_000001",
                    "reason": "ownership_conflict",
                    "payload": {"reason": "ownership_conflict"},
                }
            ]
        },
    )

    report = build_shadow_session_parity_report(
        sample_dir=sample_dir,
        actual_trace_dir=actual_dir,
        output_dir=output_dir,
        qty_tolerance=1.0,
    )

    assert report["sample_count"] == 1
    assert report["pass_count"] == 1
    assert report["fail_count"] == 0
    assert report["missing_actual_count"] == 0
    assert report["ready_for_cutin"] is True
    assert report["rows"][0]["status"] == "pass"
    assert Path(report["rows"][0]["expected_trace_path"]).exists()
    assert Path(report["rows"][0]["comparison_path"]).exists()



def test_build_shadow_session_parity_report_marks_missing_actual_trace(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    actual_dir = tmp_path / "actual"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "MSFT_20260323T104500_000001.json",
        {
            "symbol": "MSFT",
            "captured_at_et": "2026-03-23T10:45:00-04:00",
            "summary": {
                "qqq_is_traded": False,
                "qqq_exec_symbol": None,
                "index_entry_enabled": True,
                "index_final_state": "no_attempt",
            },
        },
    )

    report = build_shadow_session_parity_report(
        sample_dir=sample_dir,
        actual_trace_dir=actual_dir,
        output_dir=output_dir,
        qty_tolerance=1.0,
    )

    assert report["sample_count"] == 1
    assert report["pass_count"] == 0
    assert report["fail_count"] == 0
    assert report["missing_actual_count"] == 1
    assert report["ready_for_cutin"] is False
    assert report["rows"][0]["status"] == "missing_actual_trace"



def test_build_shadow_session_parity_report_flags_mismatch(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    actual_dir = tmp_path / "actual"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "QQQ_20260323T110000_000001.json",
        {
            "symbol": "QQQ",
            "captured_at_et": "2026-03-23T11:00:00-04:00",
            "summary": {
                "qqq_is_traded": False,
                "qqq_exec_symbol": None,
                "index_entry_enabled": True,
                "index_final_state": "no_attempt",
            },
        },
    )
    _write_json(
        actual_dir / "QQQ_20260323T110000_000001.json",
        {
            "events": [
                {
                    "stage": "entry_gate_blocked",
                    "symbol": "QQQ",
                    "trace_id": "shadow:QQQ:QQQ_20260323T110000_000001",
                    "reason": "unexpected_block",
                    "payload": {"reason": "unexpected_block"},
                }
            ]
        },
    )

    report = build_shadow_session_parity_report(
        sample_dir=sample_dir,
        actual_trace_dir=actual_dir,
        output_dir=output_dir,
        qty_tolerance=1.0,
    )

    assert report["sample_count"] == 1
    assert report["pass_count"] == 0
    assert report["fail_count"] == 1
    assert report["missing_actual_count"] == 0
    assert report["ready_for_cutin"] is False
    assert report["rows"][0]["status"] == "fail"
    assert report["rows"][0]["mismatch_count"] >= 1
