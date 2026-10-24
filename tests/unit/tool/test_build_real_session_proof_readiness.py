from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_real_session_proof_readiness import build_real_session_proof_readiness


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_real_session_proof_readiness_ready_day(tmp_path: Path) -> None:
    sample_root = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_path = tmp_path / "out" / "readiness.json"
    _write_json(
        sample_root / "2026-03-24" / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "summary": {
                "qqq_is_traded": True,
                "qqq_exec_symbol": "TQQQ",
                "index_final_state": "blocked",
                "index_gate_reason": "ownership_conflict",
            },
        },
    )
    (audit_root / "2026-03-24").mkdir(parents=True, exist_ok=True)
    (audit_root / "2026-03-24" / "strategy-10.jsonl").write_text("{}\n", encoding="utf-8")

    report = build_real_session_proof_readiness(
        sample_root=sample_root,
        audit_root=audit_root,
        output_path=output_path,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["has_any_ready_day"] is True
    assert report["rows"][0]["ready"] is True
    assert report["rows"][0]["gaps"]["total_samples_missing"] == 0
    assert report["rows"][0]["gaps"]["blocked_samples_missing"] == 0
    assert report["rows"][0]["next_actions"] == []
    assert Path(report["report_path"]).exists()


def test_build_real_session_proof_readiness_reports_missing_reasons(tmp_path: Path) -> None:
    sample_root = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_path = tmp_path / "out" / "readiness.json"
    _write_json(
        sample_root / "2026-03-24" / "AAPL_20260324T101500_000001.json",
        {
            "symbol": "AAPL",
            "summary": {
                "qqq_is_traded": False,
                "qqq_exec_symbol": "TQQQ",
                "index_final_state": "no_attempt",
            },
        },
    )

    report = build_real_session_proof_readiness(
        sample_root=sample_root,
        audit_root=audit_root,
        output_path=output_path,
        min_total_samples=2,
        min_blocked_samples=1,
    )

    row = report["rows"][0]
    assert row["ready"] is False
    assert "insufficient_total_samples" in row["reasons"]
    assert "insufficient_blocked_samples" in row["reasons"]
    assert "missing_audit_files" in row["reasons"]
    assert "no_same_symbol_qqq_samples" in row["reasons"]
    assert row["gaps"]["total_samples_missing"] == 1
    assert row["gaps"]["blocked_samples_missing"] == 1
    assert row["gaps"]["audit_files_missing"] == 1
    assert row["gaps"]["same_symbol_qqq_samples_missing"] == 1
    assert "collect_more_samples:1" in row["next_actions"]
    assert "collect_more_blocked_samples:1" in row["next_actions"]
    assert "ensure_audit_writer_and_runtime_audit_files" in row["next_actions"]
    assert "capture_same_symbol_qqq_traded_samples" in row["next_actions"]
    assert "collect_missing_samples_or_blocked_cases_for_unready_days" in report["global_next_actions"]
    assert "ensure_audit_files_exist_for_each_sample_day" in report["global_next_actions"]
    assert "rerun_real_session_proof_pipeline_after_collection" in report["global_next_actions"]


def test_build_real_session_proof_readiness_global_actions_when_no_days(tmp_path: Path) -> None:
    sample_root = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_path = tmp_path / "out" / "readiness.json"

    report = build_real_session_proof_readiness(
        sample_root=sample_root,
        audit_root=audit_root,
        output_path=output_path,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["day_count"] == 0
    assert report["has_any_ready_day"] is False
    assert "capture_real_session_samples_for_today" in report["global_next_actions"]
    assert "ensure_runtime_audit_writer_enabled" in report["global_next_actions"]
    assert "run_postclose_bundle_to_generate_readiness_inputs" in report["global_next_actions"]
