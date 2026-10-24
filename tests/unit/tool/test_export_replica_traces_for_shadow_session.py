from __future__ import annotations

import json
from pathlib import Path

from tool.replay.export_replica_traces_for_shadow_session import export_replica_traces_for_shadow_session



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def test_export_replica_traces_for_shadow_session_picks_nearest_trace(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "AAPL_20260323T103000_000001.json",
        {"symbol": "AAPL", "captured_at_et": "2026-03-23T10:30:00-04:00"},
    )
    day_dir = audit_root / "2026-03-23"
    day_dir.mkdir(parents=True)
    (day_dir / "strategy-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-23T10:05:00-04:00", "strategy_id": "index_super_win_replica_001", "symbol": "AAPL", "trace_id": "trace-old", "stage": "entry_gate_blocked", "reason": "old"}),
                json.dumps({"ts": "2026-03-23T10:29:59-04:00", "strategy_id": "index_super_win_replica_001", "symbol": "AAPL", "trace_id": "trace-new", "stage": "entry_gate_blocked", "reason": "ownership_conflict"}),
                json.dumps({"ts": "2026-03-23T10:30:01-04:00", "strategy_id": "index_super_win_replica_001", "symbol": "AAPL", "trace_id": "trace-new", "stage": "entry_gate_blocked", "reason": "ownership_conflict"}),
                json.dumps({"ts": "2026-03-23T10:30:02-04:00", "strategy_id": "other_strategy", "symbol": "AAPL", "trace_id": "trace-foreign", "stage": "entry_gate_blocked", "reason": "foreign"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    report = export_replica_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        strategy_id="index_super_win_replica_001",
    )

    assert report["exported_count"] == 1
    assert report["missing_count"] == 0
    assert report["rows"][0]["trace_id"] == "trace-new"
    exported = json.loads(Path(report["rows"][0]["output_path"]).read_text(encoding="utf-8"))
    assert exported["meta"]["trace_id"] == "trace-new"
    assert len(exported["events"]) == 2



def test_export_replica_traces_for_shadow_session_marks_missing_trace(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "MSFT_20260323T104500_000001.json",
        {"symbol": "MSFT", "captured_at_et": "2026-03-23T10:45:00-04:00"},
    )

    report = export_replica_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        strategy_id="index_super_win_replica_001",
    )

    assert report["exported_count"] == 0
    assert report["missing_count"] == 1
    assert report["rows"][0]["status"] == "missing_trace"
