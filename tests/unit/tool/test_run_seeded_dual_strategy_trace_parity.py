from __future__ import annotations

import json
from pathlib import Path

from tool.replay.run_seeded_dual_strategy_trace_parity import run_seeded_dual_strategy_trace_parity


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_run_seeded_dual_strategy_trace_parity_reports_missing_replica_trace(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    sample_dir = tmp_path / "samples"
    output_dir = tmp_path / "out"
    _write_jsonl(
        audit_root / "2026-03-24" / "strategy-10.jsonl",
        [
            {
                "ts": "2026-03-24T10:15:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "trace_id": "trace-qqq",
                "stage": "gate_evaluated",
            },
        ],
    )

    report = run_seeded_dual_strategy_trace_parity(
        day="2026-03-24",
        audit_root=audit_root,
        sample_dir=sample_dir,
        output_dir=output_dir,
        baseline_strategy_id="qqq_intraday_001",
        baseline_summary_prefix="qqq",
        baseline_source_type="qqq_trace_audit",
        underlier_aliases={"TQQQ": "QQQ", "SQQQ": "QQQ"},
        qqq_strategy_id="qqq_intraday_001",
        replica_strategy_id="index_super_win_001",
        qty_tolerance=1.0,
    )

    assert report["seed_sample_count"] == 1
    assert report["parity_sample_count"] == 1
    assert report["missing_count"] == 1
    assert report["ready_for_parity"] is False
    assert Path(report["report_path"]).exists()
    assert report["baseline_strategy_id"] == "qqq_intraday_001"
