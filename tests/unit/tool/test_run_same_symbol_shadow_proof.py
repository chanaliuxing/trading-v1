from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_same_symbol_shadow_proof import run_same_symbol_shadow_proof



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def test_run_same_symbol_shadow_proof_green(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    actual_dir = tmp_path / "actual"
    output_dir = tmp_path / "proof"
    _write_json(
        sample_dir / "AAPL_20260323T103000_000001.json",
        {
            "symbol": "AAPL",
            "captured_at_et": "2026-03-23T10:30:00-04:00",
            "exit_code": 0,
            "findings": [],
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

    proof = run_same_symbol_shadow_proof(
        sample_dir=sample_dir,
        actual_trace_dir=actual_dir,
        output_dir=output_dir,
        qty_tolerance=1.0,
    )

    assert proof["shadow_ready_for_cutin"] is True
    assert proof["parity_ready_for_cutin"] is True
    assert proof["same_symbol_proof_green"] is True
    assert Path(proof["shadow_session_report_path"]).exists()
    assert Path(proof["parity_report_path"]).exists()
    assert Path(proof["proof_path"]).exists()



def test_run_same_symbol_shadow_proof_not_green_on_missing_actual(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    actual_dir = tmp_path / "actual"
    output_dir = tmp_path / "proof"
    _write_json(
        sample_dir / "MSFT_20260323T104500_000001.json",
        {
            "symbol": "MSFT",
            "captured_at_et": "2026-03-23T10:45:00-04:00",
            "exit_code": 0,
            "findings": [],
            "summary": {
                "qqq_is_traded": False,
                "qqq_exec_symbol": None,
                "index_entry_enabled": True,
                "index_final_state": "no_attempt",
            },
        },
    )

    proof = run_same_symbol_shadow_proof(
        sample_dir=sample_dir,
        actual_trace_dir=actual_dir,
        output_dir=output_dir,
        qty_tolerance=1.0,
    )

    assert proof["shadow_ready_for_cutin"] is True
    assert proof["parity_ready_for_cutin"] is False
    assert proof["same_symbol_proof_green"] is False
    assert proof["missing_actual_count"] == 1
