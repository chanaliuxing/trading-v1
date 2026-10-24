from __future__ import annotations

from pathlib import Path

from scripts.ai_trader.research.candidate_batch_audit_summary import (
    DEFAULT_AUDIT_SUMMARY_PATH,
    build_candidate_batch_audit_summary,
)
from scripts.ai_trader.research.candidate_batch_query import query_candidate_batch_records
from scripts.ai_trader.research.candidate_batch_record import (
    DEFAULT_BATCH_RECORD_PATH,
    append_candidate_batch_record,
)
from scripts.ai_trader.research.freeze_change_gate import (
    DEFAULT_GATE_REPORT_PATH,
    evaluate_freeze_change_gate,
)
from scripts.ai_trader.research.release_signoff_summary import (
    DEFAULT_SIGNOFF_SUMMARY_PATH,
    build_release_signoff_summary,
)
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def test_readme_command_consistency() -> None:
    text = Path("docs/README.md").read_text(encoding="utf-8")
    commands = [
        "venv/bin/python -m scripts.ai_trader.research.run_fixture_cycle",
        "venv/bin/python -m scripts.ai_trader.research.freeze_change_gate",
        "venv/bin/python -m scripts.ai_trader.research.release_signoff_summary",
        "venv/bin/python -m scripts.ai_trader.research.candidate_batch_record --batch-id <batch_id> --run-id <run_id> --cycle-id fixture-2026-04-05",
        "venv/bin/python -m scripts.ai_trader.research.candidate_batch_audit_summary --keep-last-records 10",
        "venv/bin/python -m scripts.ai_trader.research.candidate_batch_query --candidate-batch-status candidate_batch_blocked --latest-n 5 --export-path runs/ai_trader/research/state/release_candidate_batch_query_export.json",
    ]
    for command in commands:
        assert command in text


def test_documented_artifact_path_consistency() -> None:
    text = Path("docs/README.md").read_text(encoding="utf-8")
    documented_paths = [
        "runs/ai_trader/research/cycles/fixture-2026-04-05/",
        DEFAULT_GATE_REPORT_PATH,
        DEFAULT_SIGNOFF_SUMMARY_PATH,
        DEFAULT_BATCH_RECORD_PATH,
        DEFAULT_AUDIT_SUMMARY_PATH,
        "runs/ai_trader/research/state/release_candidate_batch_query_export.json",
    ]
    for path_text in documented_paths:
        assert path_text in text


def test_end_to_end_artifact_existence_from_documented_sequence() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    build_release_signoff_summary()
    append_candidate_batch_record(
        batch_id="phase33-e2e",
        run_ids=["phase33-run"],
        cycle_ids=["fixture-2026-04-05"],
    )
    build_candidate_batch_audit_summary(keep_last_records=10)
    query_export = Path("runs/ai_trader/research/state/release_candidate_batch_query_export.json")
    query_candidate_batch_records(
        candidate_batch_status="candidate_batch_blocked",
        latest_n=5,
        export_path=query_export,
    )

    expected_paths = [
        Path(DEFAULT_GATE_REPORT_PATH),
        Path(DEFAULT_SIGNOFF_SUMMARY_PATH),
        Path(DEFAULT_BATCH_RECORD_PATH),
        Path(DEFAULT_AUDIT_SUMMARY_PATH),
        query_export,
    ]
    for path in expected_paths:
        assert path.exists()
        assert "runs/ai_trader/research/state" in str(path)

