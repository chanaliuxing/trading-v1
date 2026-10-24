from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_real_session_dual_proof_batch import run_real_session_dual_proof_batch


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_green_day(sample_root: Path, audit_root: Path, day: str) -> None:
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


def test_run_real_session_dual_proof_batch_green(tmp_path: Path) -> None:
    sample_root = tmp_path / "samples"
    output_root = tmp_path / "proof"
    audit_root = tmp_path / "audit"
    _write_green_day(sample_root, audit_root, "2026-03-24")

    report = run_real_session_dual_proof_batch(
        sample_root=sample_root,
        output_root=output_root,
        audit_root=audit_root,
        replica_strategy_id="index_super_win_replica_001",
        qqq_strategy_id="qqq_intraday_001",
        qty_tolerance=1.0,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["day_count"] == 1
    assert report["green_days"] == 1
    assert report["enablement_attestation_green_days"] == 1
    assert report["rows"][0]["status"] == "completed"
    assert Path(report["report_path"]).exists()


def test_run_real_session_dual_proof_batch_skips_empty_days(tmp_path: Path) -> None:
    sample_root = tmp_path / "samples"
    output_root = tmp_path / "proof"
    audit_root = tmp_path / "audit"
    (sample_root / "2026-03-24").mkdir(parents=True, exist_ok=True)
    _write_green_day(sample_root, audit_root, "2026-03-25")

    report = run_real_session_dual_proof_batch(
        sample_root=sample_root,
        output_root=output_root,
        audit_root=audit_root,
        replica_strategy_id="index_super_win_replica_001",
        qqq_strategy_id="qqq_intraday_001",
        qty_tolerance=1.0,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["day_count"] == 2
    assert report["rows"][0]["status"] == "skipped_no_samples"
    assert report["rows"][1]["status"] == "completed"
    assert report["green_days"] == 1
