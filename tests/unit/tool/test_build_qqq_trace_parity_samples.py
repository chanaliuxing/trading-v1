from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_qqq_trace_parity_samples import build_qqq_trace_parity_samples, build_trace_parity_samples


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_build_qqq_trace_parity_samples_normalizes_underlier_and_groups_by_trace(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "samples"
    _write_jsonl(
        audit_root / "2026-03-24" / "strategy-10.jsonl",
        [
            {
                "ts": "2026-03-24T10:23:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "trace_id": "trace-a",
                "trigger_id": "trg-a",
                "decision_id": "dec-a",
                "stage": "gate_evaluated",
            },
            {
                "ts": "2026-03-24T10:23:01-04:00",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "trace_id": "trace-a",
                "stage": "entry_intent_requested",
            },
            {
                "ts": "2026-03-24T10:24:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "symbol": "SQQQ",
                "trace_id": "trace-b",
                "trigger_id": "trg-b",
                "decision_id": "dec-b",
                "stage": "entry_submit_ready",
            },
        ],
    )

    manifest = build_qqq_trace_parity_samples(
        audit_root=audit_root,
        day="2026-03-24",
        output_dir=output_dir,
        strategy_id="qqq_intraday_001",
    )

    assert manifest["sample_count"] == 2
    sample_paths = sorted(p for p in output_dir.glob("*.json") if p.name != "session_manifest.json")
    assert len(sample_paths) == 2
    payload_a = json.loads(sample_paths[0].read_text(encoding="utf-8"))
    payload_b = json.loads(sample_paths[1].read_text(encoding="utf-8"))
    assert payload_a["symbol"] == "QQQ"
    assert payload_b["symbol"] == "QQQ"
    assert {payload_a["source"]["trace_id"], payload_b["source"]["trace_id"]} == {"trace-a", "trace-b"}
    assert payload_a["summary"]["index_final_state"] == "trace_expected"


def test_build_qqq_trace_parity_samples_can_preserve_exec_symbol(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "samples"
    _write_jsonl(
        audit_root / "2026-03-24" / "strategy-10.jsonl",
        [
            {
                "ts": "2026-03-24T10:23:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "symbol": "SQQQ",
                "trace_id": "trace-s",
                "stage": "gate_evaluated",
            },
        ],
    )

    manifest = build_qqq_trace_parity_samples(
        audit_root=audit_root,
        day="2026-03-24",
        output_dir=output_dir,
        strategy_id="qqq_intraday_001",
        normalize_underlier=False,
    )

    assert manifest["sample_count"] == 1
    payload = json.loads(next(output_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["symbol"] == "SQQQ"
    assert payload["summary"]["qqq_exec_symbol"] == "SQQQ"


def test_build_qqq_trace_parity_samples_filters_non_trace_and_non_strategy_rows(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "samples"
    _write_jsonl(
        audit_root / "2026-03-24" / "strategy-10.jsonl",
        [
            {
                "ts": "2026-03-24T10:23:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "trace_id": "",
                "stage": "gate_evaluated",
            },
            {
                "ts": "2026-03-24T10:23:00-04:00",
                "strategy_id": "other_strategy",
                "symbol": "TQQQ",
                "trace_id": "trace-x",
                "stage": "gate_evaluated",
            },
            {
                "ts": "2026-03-24T10:24:00-04:00",
                "strategy_id": "qqq_intraday_001",
                "symbol": "TQQQ",
                "trace_id": "trace-y",
                "stage": "entry_intent_requested",
            },
        ],
    )

    manifest = build_qqq_trace_parity_samples(
        audit_root=audit_root,
        day="2026-03-24",
        output_dir=output_dir,
        strategy_id="qqq_intraday_001",
        include_stages=("entry_intent_requested",),
    )

    assert manifest["sample_count"] == 1
    assert manifest["rows"][0]["trace_id"] == "trace-y"


def test_build_trace_parity_samples_supports_generic_prefix_and_aliases(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    output_dir = tmp_path / "samples"
    _write_jsonl(
        audit_root / "2026-03-24" / "strategy-10.jsonl",
        [
            {
                "ts": "2026-03-24T10:23:00-04:00",
                "strategy_id": "spy_intraday_001",
                "symbol": "UPRO",
                "trace_id": "trace-spy",
                "stage": "gate_evaluated",
            },
        ],
    )

    manifest = build_trace_parity_samples(
        audit_root=audit_root,
        day="2026-03-24",
        output_dir=output_dir,
        strategy_id="spy_intraday_001",
        summary_prefix="spy",
        source_type="spy_trace_audit",
        underlier_aliases={"UPRO": "SPY", "SPXU": "SPY"},
    )

    assert manifest["sample_count"] == 1
    payload = json.loads(next(path for path in output_dir.glob("*.json") if path.name != "session_manifest.json").read_text(encoding="utf-8"))
    assert payload["symbol"] == "SPY"
    assert payload["summary"]["spy_is_traded"] is True
    assert payload["summary"]["spy_exec_symbol"] == "UPRO"
    assert payload["source"]["type"] == "spy_trace_audit"
