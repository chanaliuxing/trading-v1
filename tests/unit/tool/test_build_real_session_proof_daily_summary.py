from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_real_session_proof_daily_summary import build_real_session_proof_daily_summary


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_real_session_proof_daily_summary_no_ready_days(tmp_path: Path) -> None:
    out_root = tmp_path / "proof"
    _write_json(
        out_root / "real_session_proof_readiness.json",
        {
            "has_any_ready_day": False,
            "ready_day_count": 0,
            "rows": [
                {"day": "2026-03-24", "ready": False, "reasons": ["missing_audit_files", "no_same_symbol_qqq_samples"]},
            ],
        },
    )
    _write_json(out_root / "real_session_proof_pipeline.json", {"pipeline_status": "readiness_only"})

    summary = build_real_session_proof_daily_summary(output_root=out_root)

    assert summary["needs_more_real_samples"] is True
    assert summary["blocked_day_reason_counter"]["missing_audit_files"] == 1
    assert Path(summary["report_path"]).exists()


def test_build_real_session_proof_daily_summary_with_batch_results(tmp_path: Path) -> None:
    out_root = tmp_path / "proof"
    _write_json(
        out_root / "real_session_proof_readiness.json",
        {
            "has_any_ready_day": True,
            "ready_day_count": 1,
            "rows": [
                {"day": "2026-03-24", "ready": True, "reasons": []},
                {"day": "2026-03-25", "ready": False, "reasons": ["insufficient_blocked_samples"]},
            ],
        },
    )
    _write_json(out_root / "real_session_proof_pipeline.json", {"pipeline_status": "batch_completed"})
    _write_json(
        out_root / "real_session_dual_proof_batch_report.json",
        {
            "day_count": 2,
            "green_days": 1,
            "enablement_attestation_green_days": 1,
            "rows": [
                {"day": "2026-03-24", "status": "completed", "overall_green": True, "enablement_attestation_green": True, "ownership_evidence_green": True, "samples_missing_ownership_evidence": 0},
                {"day": "2026-03-25", "status": "completed", "overall_green": False, "enablement_attestation_green": False, "ownership_evidence_green": False, "samples_missing_ownership_evidence": 2},
            ],
        },
    )

    summary = build_real_session_proof_daily_summary(output_root=out_root)

    assert summary["needs_more_real_samples"] is False
    assert summary["batch_green_days"] == 1
    assert summary["non_green_days"][0]["day"] == "2026-03-25"
    assert summary["ownership_missing_sample_total"] == 2
    assert summary["ownership_missing_days"][0]["day"] == "2026-03-25"
