from __future__ import annotations

import json
from pathlib import Path

from tool.replay.export_strategy_traces_for_shadow_session import export_strategy_traces_for_shadow_session



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def test_export_strategy_traces_for_shadow_session_supports_qqq_strategy_id(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {"symbol": "TQQQ", "captured_at_et": "2026-03-24T10:15:00-04:00"},
    )
    day_dir = audit_root / "2026-03-24"
    day_dir.mkdir(parents=True)
    (day_dir / "exec-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-24T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_intent_submitted"}),
                json.dumps({"ts": "2026-03-24T10:15:01-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_confirmed"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    report = export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        strategy_id="qqq_intraday_001",
    )

    assert report["exported_count"] == 1
    assert report["rows"][0]["trace_id"] == "trace-qqq"
    exported = json.loads(Path(report["rows"][0]["output_path"]).read_text(encoding="utf-8"))
    assert exported["meta"]["strategy_id"] == "qqq_intraday_001"
    assert len(exported["events"]) == 2


def test_export_strategy_traces_for_shadow_session_supports_symbol_aliases(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "QQQ_20260324T101500_000001.json",
        {"symbol": "QQQ", "captured_at_et": "2026-03-24T10:15:00-04:00"},
    )
    day_dir = audit_root / "2026-03-24"
    day_dir.mkdir(parents=True)
    (day_dir / "exec-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-24T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_intent_submitted"}),
                json.dumps({"ts": "2026-03-24T10:15:01-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-qqq", "stage": "entry_confirmed"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    report = export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        strategy_id="qqq_intraday_001",
        symbol_aliases={"QQQ": ["TQQQ", "SQQQ"]},
    )

    assert report["exported_count"] == 1
    assert report["rows"][0]["trace_id"] == "trace-qqq"
    assert report["rows"][0]["candidate_symbols"] == ["QQQ", "TQQQ", "SQQQ"]


def test_export_strategy_traces_for_shadow_session_prefers_decision_chain_trace(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "out"
    _write_json(
        sample_dir / "QQQ_20260324T101500_000001.json",
        {"symbol": "QQQ", "captured_at_et": "2026-03-24T10:15:00-04:00"},
    )
    day_dir = audit_root / "2026-03-24"
    day_dir.mkdir(parents=True)
    (day_dir / "exec-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-24T10:15:00-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-other", "stage": "strategy_started"}),
                json.dumps({"ts": "2026-03-24T10:14:58-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-decision", "stage": "gate_evaluated"}),
                json.dumps({"ts": "2026-03-24T10:15:01-04:00", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ", "trace_id": "trace-decision", "stage": "entry_gate_blocked"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    report = export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        strategy_id="qqq_intraday_001",
        symbol_aliases={"QQQ": ["TQQQ", "SQQQ"]},
    )

    assert report["exported_count"] == 1
    assert report["rows"][0]["trace_id"] == "trace-decision"
