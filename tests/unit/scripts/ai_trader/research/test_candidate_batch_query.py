from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.candidate_batch_query import query_candidate_batch_records
from scripts.ai_trader.research.candidate_batch_record import append_candidate_batch_record
from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_signoff_summary import build_release_signoff_summary
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_records(tmp_path: Path) -> tuple[Path, Path]:
    records_path = tmp_path / "records.json"
    audit_path = tmp_path / "audit.json"
    _write_json(
        records_path,
        {
            "records": [
                {
                    "batch_id": "batch-a",
                    "candidate_batch_status": "candidate_batch_verified",
                    "signoff_ready": True,
                    "blockers": [],
                    "recorded_at": "2026-04-11T00:00:00+00:00",
                },
                {
                    "batch_id": "batch-b",
                    "candidate_batch_status": "candidate_batch_blocked",
                    "signoff_ready": False,
                    "blockers": ["preflight_not_passed"],
                    "recorded_at": "2026-04-11T00:01:00+00:00",
                },
                {
                    "batch_id": "batch-c",
                    "candidate_batch_status": "candidate_batch_blocked",
                    "signoff_ready": False,
                    "blockers": ["freeze_gate_not_passed"],
                    "recorded_at": "2026-04-11T00:02:00+00:00",
                },
            ]
        },
    )
    _write_json(audit_path, {"status": "ok", "total_records": 3})
    return records_path, audit_path


def test_query_by_batch_id(tmp_path: Path) -> None:
    records_path, audit_path = _seed_records(tmp_path)
    result = query_candidate_batch_records(
        batch_id="batch-b",
        records_path=records_path,
        audit_summary_path=audit_path,
    )
    assert result["status"] == "ok"
    assert result["total_matches"] == 1
    assert result["latest_match"]["batch_id"] == "batch-b"


def test_query_by_status(tmp_path: Path) -> None:
    records_path, audit_path = _seed_records(tmp_path)
    result = query_candidate_batch_records(
        candidate_batch_status="candidate_batch_blocked",
        records_path=records_path,
        audit_summary_path=audit_path,
    )
    assert result["status"] == "ok"
    assert result["total_matches"] == 2
    assert all(r["candidate_batch_status"] == "candidate_batch_blocked" for r in result["matched_records"])


def test_query_latest_n(tmp_path: Path) -> None:
    records_path, audit_path = _seed_records(tmp_path)
    result = query_candidate_batch_records(
        latest_n=2,
        records_path=records_path,
        audit_summary_path=audit_path,
    )
    assert result["status"] == "ok"
    assert result["total_matches"] == 2
    assert result["matched_records"][0]["batch_id"] == "batch-b"
    assert result["matched_records"][1]["batch_id"] == "batch-c"


def test_empty_result_and_time_filter(tmp_path: Path) -> None:
    records_path, audit_path = _seed_records(tmp_path)
    result = query_candidate_batch_records(
        start_time="2026-04-11T01:00:00+00:00",
        end_time="2026-04-11T02:00:00+00:00",
        records_path=records_path,
        audit_summary_path=audit_path,
    )
    assert result["status"] == "ok"
    assert result["total_matches"] == 0
    assert result["latest_match"] == {}


def test_missing_and_corrupt_fallback(tmp_path: Path) -> None:
    missing = query_candidate_batch_records(
        records_path=tmp_path / "missing.json",
        audit_summary_path=tmp_path / "missing_audit.json",
    )
    assert missing["status"] == "unavailable"
    assert missing["reason"] == "missing_record_file"

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{bad-json", encoding="utf-8")
    degraded = query_candidate_batch_records(
        records_path=corrupt,
        audit_summary_path=tmp_path / "missing_audit.json",
    )
    assert degraded["status"] == "degraded"
    assert degraded["reason"] == "corrupt_record_file"


def test_export_output_integrity_and_real_fixture_export() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    build_release_signoff_summary()
    append_candidate_batch_record(
        batch_id="phase32-query",
        run_ids=["phase32-run"],
        cycle_ids=["fixture-2026-04-05"],
    )
    export_path = Path("runs/ai_trader/research/state/release_candidate_batch_query_export.json")
    result = query_candidate_batch_records(
        latest_n=3,
        export_path=export_path,
    )
    assert result["status"] in {"ok", "unavailable", "degraded"}
    assert export_path.exists()
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert set(exported.keys()) >= {
        "matched_records",
        "total_matches",
        "latest_match",
        "applied_filters",
        "export_path",
    }
    assert exported["export_path"] == str(export_path)

