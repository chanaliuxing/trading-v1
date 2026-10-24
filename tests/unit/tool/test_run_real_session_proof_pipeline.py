from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_real_session_proof_pipeline import run_real_session_proof_pipeline


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_ready_day(sample_root: Path, audit_root: Path, day: str) -> None:
    _write_json(
        sample_root / day / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": f"{day}T10:15:00-04:00",
            "exit_code": 0,
            "findings": [],
            "summary": {
                "qqq_is_traded": True,
                "qqq_exec_symbol": "TQQQ",
                "index_final_state": "blocked",
                "index_gate_reason": "ownership_conflict",
            },
        },
    )
    day_dir = audit_root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "strategy-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": f"{day}T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_intent_submitted"}),
                json.dumps({"ts": f"{day}T10:14:59-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_confirmed"}),
                json.dumps({"ts": f"{day}T10:15:00-04:00", "strategy_id": "index_super_win_replica_001", "symbol": "TQQQ", "trace_id": "trace-replica", "stage": "entry_gate_blocked", "reason": "ownership_conflict"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )


def test_run_real_session_proof_pipeline_readiness_only_when_no_ready_days(tmp_path: Path) -> None:
    sample_root = tmp_path / "samples"
    output_root = tmp_path / "proof"
    audit_root = tmp_path / "audit"
    _write_json(
        sample_root / "2026-03-24" / "AAPL_20260324T101500_000001.json",
        {"symbol": "AAPL", "summary": {"qqq_is_traded": False, "qqq_exec_symbol": "TQQQ", "index_final_state": "no_attempt"}},
    )

    report = run_real_session_proof_pipeline(
        sample_root=sample_root,
        output_root=output_root,
        audit_root=audit_root,
        replica_strategy_id="index_super_win_replica_001",
        qqq_strategy_id="qqq_intraday_001",
        qty_tolerance=1.0,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["pipeline_status"] == "readiness_only"
    assert report["ready_days"] == []
    assert report["batch_report_path"] is None
    assert Path(report["report_path"]).exists()


def test_run_real_session_proof_pipeline_runs_batch_for_ready_days(tmp_path: Path) -> None:
    sample_root = tmp_path / "samples"
    output_root = tmp_path / "proof"
    audit_root = tmp_path / "audit"
    _write_ready_day(sample_root, audit_root, "2026-03-24")

    report = run_real_session_proof_pipeline(
        sample_root=sample_root,
        output_root=output_root,
        audit_root=audit_root,
        replica_strategy_id="index_super_win_replica_001",
        qqq_strategy_id="qqq_intraday_001",
        qty_tolerance=1.0,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["pipeline_status"] == "batch_completed"
    assert report["ready_days"] == ["2026-03-24"]
    assert report["selected_days"] == ["2026-03-24"]
    assert report["batch_green"] is True
    assert Path(str(report["batch_report_path"])).exists()
