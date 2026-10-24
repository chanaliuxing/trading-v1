from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_replica_trace_smoke_gate import build_replica_trace_smoke_gate


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_build_replica_trace_smoke_gate_green_when_system_and_decision_stages_present(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    _write_jsonl(
        audit_root / "2026-03-25" / "strategy-10.jsonl",
        [
            {"ts": "2026-03-25T09:29:59-04:00", "strategy_id": "index_super_win_001", "stage": "strategy_started"},
            {"ts": "2026-03-25T09:30:00-04:00", "strategy_id": "index_super_win_001", "stage": "strategy_ready"},
            {"ts": "2026-03-25T09:45:00-04:00", "strategy_id": "index_super_win_001", "stage": "gate_evaluated", "trace_id": "t1", "symbol": "TQQQ"},
        ],
    )

    report = build_replica_trace_smoke_gate(
        audit_root=audit_root,
        day="2026-03-25",
        strategy_id="index_super_win_001",
    )

    assert report["ready_for_trace_parity_capture"] is True
    assert report["checks"]["system_stages_present"] is True
    assert report["checks"]["decision_chain_stage_present"] is True
    assert report["observed"]["trace_count"] == 1
    assert report["observed"]["decision_symbols"] == ["TQQQ"]


def test_build_replica_trace_smoke_gate_red_when_decision_chain_missing(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    _write_jsonl(
        audit_root / "2026-03-25" / "system-09.jsonl",
        [
            {"ts": "2026-03-25T09:29:59-04:00", "strategy_id": "index_super_win_001", "stage": "strategy_started"},
            {"ts": "2026-03-25T09:30:00-04:00", "strategy_id": "index_super_win_001", "stage": "strategy_ready"},
        ],
    )

    report = build_replica_trace_smoke_gate(
        audit_root=audit_root,
        day="2026-03-25",
        strategy_id="index_super_win_001",
    )

    assert report["ready_for_trace_parity_capture"] is False
    assert report["checks"]["system_stages_present"] is True
    assert report["checks"]["decision_chain_stage_present"] is False


def test_build_replica_trace_smoke_gate_red_when_system_stage_missing(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    _write_jsonl(
        audit_root / "2026-03-25" / "strategy-10.jsonl",
        [
            {"ts": "2026-03-25T09:45:00-04:00", "strategy_id": "index_super_win_001", "stage": "entry_gate_blocked", "trace_id": "t1", "symbol": "TQQQ"},
        ],
    )

    report = build_replica_trace_smoke_gate(
        audit_root=audit_root,
        day="2026-03-25",
        strategy_id="index_super_win_001",
    )

    assert report["ready_for_trace_parity_capture"] is False
    assert report["checks"]["system_stages_present"] is False
    assert report["checks"]["decision_chain_stage_present"] is True
