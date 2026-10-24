from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_dual_strategy_same_symbol_proof import run_dual_strategy_same_symbol_proof



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def test_run_dual_strategy_same_symbol_proof_green(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
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
    day_dir = audit_root / "2026-03-24"
    day_dir.mkdir(parents=True)
    (day_dir / "strategy-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-24T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_intent_submitted"}),
                json.dumps({"ts": "2026-03-24T10:14:59-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_confirmed"}),
                json.dumps({"ts": "2026-03-24T10:15:00-04:00", "strategy_id": "index_super_win_replica_001", "symbol": "TQQQ", "trace_id": "trace-replica", "stage": "entry_gate_blocked", "reason": "ownership_conflict"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    report = run_dual_strategy_same_symbol_proof(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_replica_001",
        qty_tolerance=1.0,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["qqq_exported_count"] == 1
    assert report["replica_exported_count"] == 1
    assert report["qqq_expectation_ok"] is True
    assert report["replica_same_symbol_proof_green"] is True
    assert report["summary_compare_green"] is True
    assert report["foreign_safety_green"] is True
    assert report["enablement_attestation_green"] is True
    assert report["overall_green"] is True
    assert Path(report["report_path"]).exists()



def test_run_dual_strategy_same_symbol_proof_not_green_when_qqq_missing(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
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
    day_dir = audit_root / "2026-03-24"
    day_dir.mkdir(parents=True)
    (day_dir / "strategy-10.jsonl").write_text(
        json.dumps({"ts": "2026-03-24T10:15:00-04:00", "strategy_id": "index_super_win_replica_001", "symbol": "TQQQ", "trace_id": "trace-replica", "stage": "entry_gate_blocked", "reason": "ownership_conflict"}) + "\n",
        encoding="utf-8",
    )

    report = run_dual_strategy_same_symbol_proof(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_replica_001",
        qty_tolerance=1.0,
        min_total_samples=1,
        min_blocked_samples=1,
    )

    assert report["qqq_missing_count"] == 1
    assert report["replica_exported_count"] == 1
    assert report["qqq_expectation_ok"] is False
    assert report["replica_same_symbol_proof_green"] is True
    assert report["summary_compare_green"] is True
    assert report["foreign_safety_green"] is True
    assert report["overall_green"] is False



def test_run_dual_strategy_same_symbol_proof_allows_no_qqq_trace_when_sample_not_live(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "AAPL_20260324T101500_000001.json",
        {
            "symbol": "AAPL",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "exit_code": 0,
            "findings": [],
            "summary": {
                "qqq_is_traded": False,
                "qqq_exec_symbol": "TQQQ",
                "index_entry_enabled": True,
                "index_final_state": "no_attempt",
            },
        },
    )
    day_dir = audit_root / "2026-03-24"
    day_dir.mkdir(parents=True)
    (day_dir / "strategy-10.jsonl").write_text(
        json.dumps({"ts": "2026-03-24T10:15:00-04:00", "strategy_id": "index_super_win_replica_001", "symbol": "AAPL", "trace_id": "trace-replica", "stage": "entry_gate_allowed", "reason": "entry_enabled_no_conflict"}) + "\n",
        encoding="utf-8",
    )

    report = run_dual_strategy_same_symbol_proof(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_replica_001",
        qty_tolerance=1.0,
        min_total_samples=2,
        min_blocked_samples=1,
    )

    assert report["qqq_missing_count"] == 1
    assert report["qqq_expectation_ok"] is True
    assert report["replica_same_symbol_proof_green"] is True
    assert report["enablement_attestation_green"] is False
    assert report["overall_green"] is False
