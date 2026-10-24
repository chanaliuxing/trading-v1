from __future__ import annotations

import json
from pathlib import Path

from tool.replay.build_dual_strategy_enablement_attestation import build_dual_strategy_enablement_attestation


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_build_dual_strategy_enablement_attestation_green(tmp_path: Path) -> None:
    out_dir = tmp_path / "proof"
    foreign_path = out_dir / "dual_strategy_foreign_safety_report.json"
    _write_json(
        foreign_path,
        {
            "blocked_sample_count": 2,
            "blocked_samples_with_forbidden_replica_actions": 0,
            "samples_missing_ownership_evidence": 0,
            "audit_high_risk_counts": {
                "fill_attribution_ambiguous": 0,
                "fill_attribution_fallback_broker_order_id": 0,
                "cancelled_broker_ghost_order": 0,
            },
        },
    )
    dual_path = out_dir / "dual_strategy_same_symbol_proof.json"
    _write_json(
        dual_path,
        {
            "qqq_expectation_report": {"sample_count": 3},
            "replica_same_symbol_proof_green": True,
            "qqq_expectation_ok": True,
            "summary_compare_green": True,
            "foreign_safety_green": True,
            "foreign_safety_report_path": str(foreign_path),
        },
    )

    report = build_dual_strategy_enablement_attestation(
        dual_proof_report_path=dual_path,
        output_dir=out_dir,
        min_total_samples=2,
        min_blocked_samples=1,
    )

    assert report["checks"]["evidence_sufficient"] is True
    assert report["checks"]["zero_foreign_flatten"] is True
    assert report["checks"]["zero_foreign_ledger_attribution"] is True
    assert report["checks"]["ownership_evidence_complete"] is True
    assert report["attestation_green"] is True
    assert Path(report["report_path"]).exists()


def test_build_dual_strategy_enablement_attestation_requires_sufficient_clean_evidence(tmp_path: Path) -> None:
    out_dir = tmp_path / "proof"
    foreign_path = out_dir / "dual_strategy_foreign_safety_report.json"
    _write_json(
        foreign_path,
        {
            "blocked_sample_count": 0,
            "blocked_samples_with_forbidden_replica_actions": 1,
            "samples_missing_ownership_evidence": 1,
            "audit_high_risk_counts": {
                "fill_attribution_ambiguous": 1,
                "fill_attribution_fallback_broker_order_id": 0,
                "cancelled_broker_ghost_order": 1,
            },
        },
    )
    dual_path = out_dir / "dual_strategy_same_symbol_proof.json"
    _write_json(
        dual_path,
        {
            "qqq_expectation_report": {"sample_count": 1},
            "replica_same_symbol_proof_green": True,
            "qqq_expectation_ok": True,
            "summary_compare_green": True,
            "foreign_safety_green": False,
            "foreign_safety_report_path": str(foreign_path),
        },
    )

    report = build_dual_strategy_enablement_attestation(
        dual_proof_report_path=dual_path,
        output_dir=out_dir,
        min_total_samples=2,
        min_blocked_samples=1,
    )

    assert report["checks"]["evidence_sufficient"] is False
    assert report["checks"]["zero_foreign_cancel"] is False
    assert report["checks"]["zero_foreign_flatten"] is False
    assert report["checks"]["zero_foreign_ledger_attribution"] is False
    assert report["checks"]["ownership_evidence_complete"] is False
    assert report["attestation_green"] is False
