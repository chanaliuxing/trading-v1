#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_dual_strategy_enablement_attestation(
    *,
    dual_proof_report_path: Path,
    output_dir: Path,
    min_total_samples: int = 1,
    min_blocked_samples: int = 1,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dual = _load_json(dual_proof_report_path)

    foreign_path = Path(str(dual.get("foreign_safety_report_path") or "")).expanduser()
    foreign = _load_json(foreign_path) if foreign_path.exists() else {}

    qqq_expectation = dict(dual.get("qqq_expectation_report") or {})
    summary_compare_green = bool(dual.get("summary_compare_green", False))
    replica_proof_green = bool(dual.get("replica_same_symbol_proof_green", False))
    foreign_safety_green = bool(dual.get("foreign_safety_green", False))
    qqq_expectation_ok = bool(dual.get("qqq_expectation_ok", False))

    total_samples = int(qqq_expectation.get("sample_count", 0) or 0)
    blocked_samples = int(foreign.get("blocked_sample_count", 0) or 0)
    forbidden_blocked = int(foreign.get("blocked_samples_with_forbidden_replica_actions", 0) or 0)
    missing_ownership_evidence = int(foreign.get("samples_missing_ownership_evidence", 0) or 0)
    high_risk_counts = dict(foreign.get("audit_high_risk_counts") or {})
    ambiguous = int(high_risk_counts.get("fill_attribution_ambiguous", 0) or 0)
    fallback = int(high_risk_counts.get("fill_attribution_fallback_broker_order_id", 0) or 0)
    ghost_cancel = int(high_risk_counts.get("cancelled_broker_ghost_order", 0) or 0)

    evidence_sufficient = total_samples >= int(min_total_samples) and blocked_samples >= int(min_blocked_samples)
    zero_foreign_cancel = ghost_cancel == 0
    zero_foreign_flatten = forbidden_blocked == 0
    zero_foreign_ledger_attribution = ambiguous == 0 and fallback == 0
    ownership_evidence_complete = missing_ownership_evidence == 0

    checks = {
        "replica_same_symbol_proof_green": replica_proof_green,
        "qqq_expectation_ok": qqq_expectation_ok,
        "summary_compare_green": summary_compare_green,
        "foreign_safety_green": foreign_safety_green,
        "evidence_sufficient": evidence_sufficient,
        "zero_foreign_cancel": zero_foreign_cancel,
        "zero_foreign_flatten": zero_foreign_flatten,
        "zero_foreign_ledger_attribution": zero_foreign_ledger_attribution,
        "ownership_evidence_complete": ownership_evidence_complete,
    }
    attestation_green = all(bool(value) for value in checks.values())

    report = {
        "dual_proof_report_path": str(dual_proof_report_path),
        "foreign_safety_report_path": str(foreign_path) if foreign_path else None,
        "min_total_samples": int(min_total_samples),
        "min_blocked_samples": int(min_blocked_samples),
        "observed": {
            "total_samples": total_samples,
            "blocked_samples": blocked_samples,
            "blocked_samples_with_forbidden_replica_actions": forbidden_blocked,
            "samples_missing_ownership_evidence": missing_ownership_evidence,
            "fill_attribution_ambiguous": ambiguous,
            "fill_attribution_fallback_broker_order_id": fallback,
            "cancelled_broker_ghost_order": ghost_cancel,
        },
        "checks": checks,
        "attestation_green": attestation_green,
    }
    report_path = output_dir / "dual_strategy_enablement_attestation.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build enablement attestation from a dual-strategy same-symbol proof package")
    parser.add_argument("--dual-proof-report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--min-total-samples", type=int, default=1)
    parser.add_argument("--min-blocked-samples", type=int, default=1)
    args = parser.parse_args()

    report = build_dual_strategy_enablement_attestation(
        dual_proof_report_path=Path(args.dual_proof_report),
        output_dir=Path(args.output_dir),
        min_total_samples=int(args.min_total_samples),
        min_blocked_samples=int(args.min_blocked_samples),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("attestation_green")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
