from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_dual_strategy_foreign_safety_report import build_dual_strategy_foreign_safety_report



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")



def test_build_dual_strategy_foreign_safety_report_green(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    qqq_dir = tmp_path / "qqq"
    replica_dir = tmp_path / "replica"
    audit_root = tmp_path / "audit"
    out_dir = tmp_path / "out"

    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "summary": {"index_final_state": "blocked", "index_gate_reason": "ownership_conflict"},
        },
    )
    _write_json(replica_dir / "TQQQ_20260324T101500_000001.json", {"events": [{"stage": "entry_gate_blocked", "reason": "ownership_conflict"}]})
    _write_json(qqq_dir / "TQQQ_20260324T101500_000001.json", {"events": [{"stage": "entry_intent_submitted", "reason": "qqq_intraday_entry"}]})
    (audit_root / "2026-03-24").mkdir(parents=True)

    report = build_dual_strategy_foreign_safety_report(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_dir,
        replica_trace_dir=replica_dir,
        audit_root=audit_root,
        output_dir=out_dir,
    )

    assert report["blocked_sample_count"] == 1
    assert report["blocked_samples_with_forbidden_replica_actions"] == 0
    assert report["samples_missing_ownership_evidence"] == 0
    assert report["foreign_safety_green"] is True
    assert Path(report["report_path"]).exists()



def test_build_dual_strategy_foreign_safety_report_flags_forbidden_and_attribution_risk(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    qqq_dir = tmp_path / "qqq"
    replica_dir = tmp_path / "replica"
    audit_root = tmp_path / "audit"
    out_dir = tmp_path / "out"

    _write_json(
        sample_dir / "TQQQ_20260324T101500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:15:00-04:00",
            "summary": {"index_final_state": "blocked", "index_gate_reason": "ownership_conflict"},
        },
    )
    _write_json(
        replica_dir / "TQQQ_20260324T101500_000001.json",
        {"events": [{"ts": "2026-03-24T10:15:01-04:00", "stage": "flatten_submitted", "reason": "logic_retreat"}]},
    )
    _write_json(qqq_dir / "TQQQ_20260324T101500_000001.json", {"events": []})
    day_dir = audit_root / "2026-03-24"
    day_dir.mkdir(parents=True)
    (day_dir / "exec-10.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-24T10:15:02-04:00", "stage": "fill_attribution_ambiguous", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ"}),
                json.dumps({"ts": "2026-03-24T10:15:03-04:00", "stage": "fill_attribution_fallback_broker_order_id", "strategy_id": "qqq_intraday_001", "symbol": "TQQQ"}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    report = build_dual_strategy_foreign_safety_report(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_dir,
        replica_trace_dir=replica_dir,
        audit_root=audit_root,
        output_dir=out_dir,
    )

    assert report["blocked_samples_with_forbidden_replica_actions"] == 1
    assert report["audit_high_risk_counts"]["fill_attribution_ambiguous"] == 1
    assert report["audit_high_risk_counts"]["fill_attribution_fallback_broker_order_id"] == 1
    assert report["foreign_safety_green"] is False


def test_build_dual_strategy_foreign_safety_report_flags_missing_ownership_evidence(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    qqq_dir = tmp_path / "qqq"
    replica_dir = tmp_path / "replica"
    audit_root = tmp_path / "audit"
    out_dir = tmp_path / "out"

    _write_json(
        sample_dir / "TQQQ_20260324T102000_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:20:00-04:00",
            "summary": {"index_final_state": "entered"},
        },
    )
    _write_json(
        replica_dir / "TQQQ_20260324T102000_000001.json",
        {"events": [{"ts": "2026-03-24T10:20:01-04:00", "stage": "entry_confirmed", "reason": "order_update:filled"}]},
    )
    _write_json(qqq_dir / "TQQQ_20260324T102000_000001.json", {"events": []})
    (audit_root / "2026-03-24").mkdir(parents=True)

    report = build_dual_strategy_foreign_safety_report(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_dir,
        replica_trace_dir=replica_dir,
        audit_root=audit_root,
        output_dir=out_dir,
    )

    assert report["samples_missing_ownership_evidence"] == 1
    assert report["foreign_safety_green"] is False
    assert report["ownership_rows"][0]["ownership_evidence_ok"] is False


def test_build_dual_strategy_foreign_safety_report_accepts_ownership_evidence(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    qqq_dir = tmp_path / "qqq"
    replica_dir = tmp_path / "replica"
    audit_root = tmp_path / "audit"
    out_dir = tmp_path / "out"

    _write_json(
        sample_dir / "TQQQ_20260324T102500_000001.json",
        {
            "symbol": "TQQQ",
            "captured_at_et": "2026-03-24T10:25:00-04:00",
            "summary": {"index_final_state": "entered"},
        },
    )
    _write_json(
        replica_dir / "TQQQ_20260324T102500_000001.json",
        {
            "events": [
                {"ts": "2026-03-24T10:25:01-04:00", "stage": "entry_confirmed", "reason": "order_update:filled"},
                {"ts": "2026-03-24T10:25:01-04:00", "stage": "ownership_entry_lot_live", "reason": "entry_confirmed"},
            ]
        },
    )
    _write_json(qqq_dir / "TQQQ_20260324T102500_000001.json", {"events": []})
    (audit_root / "2026-03-24").mkdir(parents=True)

    report = build_dual_strategy_foreign_safety_report(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_dir,
        replica_trace_dir=replica_dir,
        audit_root=audit_root,
        output_dir=out_dir,
    )

    assert report["samples_missing_ownership_evidence"] == 0
    assert report["ownership_rows"][0]["ownership_evidence_ok"] is True
