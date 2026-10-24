from __future__ import annotations

import json
from pathlib import Path

from scripts.ai_trader.research.candidate_batch_record import append_candidate_batch_record
from scripts.ai_trader.research.freeze_change_gate import evaluate_freeze_change_gate
from scripts.ai_trader.research.release_signoff_summary import build_release_signoff_summary
from scripts.ai_trader.research.run_fixture_cycle import main as run_fixture_cycle_main


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_reports(tmp_path: Path, *, signoff_ready: bool, gate_status: str = "pass", preflight_status: str = "pass") -> tuple[Path, Path, Path, Path]:
    state = tmp_path / "runs/ai_trader/research/state"
    preflight = state / "preflight_release_check.json"
    gate = state / "research_safety_contract_gate_report.json"
    signoff = state / "release_signoff_summary.json"
    output = state / "release_candidate_batch_records.json"

    _write_json(preflight, {"status": preflight_status, "overall_status": "ready" if preflight_status == "pass" else "blocked"})
    _write_json(gate, {"gate_status": gate_status, "execution_guard_status": {"passed": True, "checks": []}})
    _write_json(
        signoff,
        {
            "preflight_status": preflight_status,
            "freeze_gate_status": gate_status,
            "execution_guard_status": {"passed": True, "checks": []},
            "required_artifacts_present": True,
            "signoff_ready": signoff_ready,
            "blockers": [] if signoff_ready else ["example_blocker"],
            "recommended_actions": [],
        },
    )
    return preflight, gate, signoff, output


def test_verified_batch_record() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        tmp = Path(tdir)
        preflight, gate, signoff, output = _seed_reports(tmp, signoff_ready=True)
        result = append_candidate_batch_record(
            batch_id="batch-001",
            run_ids=["run-a"],
            cycle_ids=["cycle-a"],
            preflight_release_check_path=preflight,
            freeze_gate_report_path=gate,
            release_signoff_summary_path=signoff,
            output_path=output,
        )
        entry = result["entry"]
        assert entry["candidate_batch_status"] == "candidate_batch_verified"
        assert entry["signoff_ready"] is True


def test_blocked_batch_record() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        tmp = Path(tdir)
        preflight, gate, signoff, output = _seed_reports(tmp, signoff_ready=False, gate_status="hold")
        result = append_candidate_batch_record(
            batch_id="batch-002",
            preflight_release_check_path=preflight,
            freeze_gate_report_path=gate,
            release_signoff_summary_path=signoff,
            output_path=output,
        )
        entry = result["entry"]
        assert entry["candidate_batch_status"] == "candidate_batch_blocked"
        assert entry["signoff_ready"] is False


def test_append_history_record_without_overwrite() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        tmp = Path(tdir)
        preflight, gate, signoff, output = _seed_reports(tmp, signoff_ready=True)
        first = append_candidate_batch_record(
            batch_id="batch-003",
            run_ids=["run-1"],
            cycle_ids=["cycle-1"],
            preflight_release_check_path=preflight,
            freeze_gate_report_path=gate,
            release_signoff_summary_path=signoff,
            output_path=output,
            recorded_at="2026-04-11T00:00:00+00:00",
        )
        second = append_candidate_batch_record(
            batch_id="batch-003",
            run_ids=["run-2"],
            cycle_ids=["cycle-2"],
            preflight_release_check_path=preflight,
            freeze_gate_report_path=gate,
            release_signoff_summary_path=signoff,
            output_path=output,
            recorded_at="2026-04-11T00:01:00+00:00",
        )
        payload = json.loads(Path(output).read_text(encoding="utf-8"))
        assert first["records_count"] == 1
        assert second["records_count"] == 2
        assert len(payload["records"]) == 2
        assert payload["records"][0]["recorded_at"] == "2026-04-11T00:00:00+00:00"
        assert payload["records"][1]["recorded_at"] == "2026-04-11T00:01:00+00:00"


def test_missing_report_fallback_records_blocked() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        tmp = Path(tdir)
        preflight, gate, signoff, output = _seed_reports(tmp, signoff_ready=True)
        Path(signoff).unlink(missing_ok=True)
        result = append_candidate_batch_record(
            batch_id="batch-004",
            preflight_release_check_path=preflight,
            freeze_gate_report_path=gate,
            release_signoff_summary_path=signoff,
            output_path=output,
        )
        entry = result["entry"]
        assert entry["candidate_batch_status"] == "candidate_batch_blocked"
        assert any(str(x).startswith("missing_report:") for x in entry["blockers"])


def test_artifact_path_integrity() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tdir:
        tmp = Path(tdir)
        preflight, gate, signoff, output = _seed_reports(tmp, signoff_ready=True)
        result = append_candidate_batch_record(
            batch_id="batch-005",
            preflight_release_check_path=preflight,
            freeze_gate_report_path=gate,
            release_signoff_summary_path=signoff,
            output_path=output,
        )
        integrity = result["entry"]["artifact_path_integrity"]
        assert set(integrity.keys()) == {"preflight_release_check", "freeze_gate_report", "release_signoff_summary"}
        assert all(bool(v) for v in integrity.values())


def test_real_fixture_batch_record_persisted() -> None:
    run_fixture_cycle_main()
    evaluate_freeze_change_gate()
    build_release_signoff_summary()
    result = append_candidate_batch_record(
        batch_id="fixture-batch-2026-04-05",
        run_ids=["fixture-run-2026-04-05"],
        cycle_ids=["fixture-2026-04-05"],
    )
    output_path = Path(result["release_candidate_batch_records_json"])
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload.get("latest", {}).get("batch_id") == "fixture-batch-2026-04-05"

