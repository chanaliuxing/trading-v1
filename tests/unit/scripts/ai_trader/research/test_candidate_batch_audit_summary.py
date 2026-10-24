from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.candidate_batch_audit_summary import build_candidate_batch_audit_summary
from scripts.ai_trader.research.candidate_batch_record import append_candidate_batch_record
from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_signoff_summary import build_release_signoff_summary
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def test_audit_summary_aggregation_and_counts(tmp_path: Path) -> None:
    records_path = tmp_path / "records.json"
    output_path = tmp_path / "audit.json"
    _write_json(
        records_path,
        {
            "records": [
                {"batch_id": "b1", "candidate_batch_status": "candidate_batch_verified", "signoff_ready": True, "blockers": [], "recorded_at": "2026-04-11T00:00:00+00:00"},
                {"batch_id": "b2", "candidate_batch_status": "candidate_batch_blocked", "signoff_ready": False, "blockers": ["x"], "recorded_at": "2026-04-11T00:01:00+00:00"},
                {"batch_id": "b3", "candidate_batch_status": "candidate_batch_verified", "signoff_ready": True, "blockers": [], "recorded_at": "2026-04-11T00:02:00+00:00"},
            ]
        },
    )
    summary = build_candidate_batch_audit_summary(batch_record_path=records_path, output_path=output_path)[
        "release_candidate_batch_audit_summary"
    ]
    assert summary["status"] == "ok"
    assert summary["total_records"] == 3
    assert summary["verified_count"] == 2
    assert summary["blocked_count"] == 1
    assert summary["latest_batch"]["batch_id"] == "b3"


def test_retention_window_prune_and_latest_protected(tmp_path: Path) -> None:
    records_path = tmp_path / "records.json"
    output_path = tmp_path / "audit.json"
    _write_json(
        records_path,
        {
            "records": [
                {"batch_id": "b1", "candidate_batch_status": "candidate_batch_verified", "signoff_ready": True, "blockers": [], "recorded_at": "2026-04-11T00:00:00+00:00"},
                {"batch_id": "b2", "candidate_batch_status": "candidate_batch_blocked", "signoff_ready": False, "blockers": ["x"], "recorded_at": "2026-04-11T00:01:00+00:00"},
                {"batch_id": "b3", "candidate_batch_status": "candidate_batch_blocked", "signoff_ready": False, "blockers": ["y"], "recorded_at": "2026-04-11T00:02:00+00:00"},
            ]
        },
    )
    summary = build_candidate_batch_audit_summary(
        batch_record_path=records_path,
        output_path=output_path,
        keep_last_records=2,
    )["release_candidate_batch_audit_summary"]
    assert summary["total_records"] == 2
    assert summary["retention"]["pruned_records_count"] == 1
    assert summary["retention"]["latest_record_protected"] is True
    assert summary["latest_batch"]["batch_id"] == "b3"


def test_retention_keep_last_batches(tmp_path: Path) -> None:
    records_path = tmp_path / "records.json"
    output_path = tmp_path / "audit.json"
    _write_json(
        records_path,
        {
            "records": [
                {"batch_id": "b1", "candidate_batch_status": "candidate_batch_blocked", "signoff_ready": False, "blockers": ["x"], "recorded_at": "2026-04-11T00:00:00+00:00"},
                {"batch_id": "b2", "candidate_batch_status": "candidate_batch_verified", "signoff_ready": True, "blockers": [], "recorded_at": "2026-04-11T00:01:00+00:00"},
                {"batch_id": "b2", "candidate_batch_status": "candidate_batch_verified", "signoff_ready": True, "blockers": [], "recorded_at": "2026-04-11T00:02:00+00:00"},
                {"batch_id": "b3", "candidate_batch_status": "candidate_batch_blocked", "signoff_ready": False, "blockers": ["y"], "recorded_at": "2026-04-11T00:03:00+00:00"},
            ]
        },
    )
    summary = build_candidate_batch_audit_summary(
        batch_record_path=records_path,
        output_path=output_path,
        keep_last_batches=2,
    )["release_candidate_batch_audit_summary"]
    assert summary["retention"]["mode"] == "batch_window"
    assert summary["retention"]["pruned_records_count"] >= 1
    assert "b1" in summary["retention"]["pruned_batch_ids"]
    recent_ids = {row["batch_id"] for row in summary["recent_batches"]}
    assert "b3" in recent_ids


def test_missing_and_corrupt_record_file_fallback(tmp_path: Path) -> None:
    missing = build_candidate_batch_audit_summary(
        batch_record_path=tmp_path / "missing.json",
        output_path=tmp_path / "audit_missing.json",
    )["release_candidate_batch_audit_summary"]
    assert missing["status"] == "unavailable"
    assert missing["reason"] == "missing_record_file"

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{bad-json", encoding="utf-8")
    corrupt = build_candidate_batch_audit_summary(
        batch_record_path=corrupt_path,
        output_path=tmp_path / "audit_corrupt.json",
    )["release_candidate_batch_audit_summary"]
    assert corrupt["status"] == "degraded"
    assert corrupt["reason"] == "corrupt_record_file"


def test_real_fixture_audit_summary_persisted() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    build_release_signoff_summary()
    append_candidate_batch_record(
        batch_id="phase31-audit",
        run_ids=["phase31-run"],
        cycle_ids=["fixture-2026-04-05"],
    )
    bundle = build_candidate_batch_audit_summary()
    path = Path(bundle["release_candidate_batch_audit_summary_json"])
    assert path.exists()
    payload = bundle["release_candidate_batch_audit_summary"]
    assert set(payload.keys()) >= {
        "latest_batch",
        "total_records",
        "verified_count",
        "blocked_count",
        "latest_blockers",
        "recent_batches",
    }

