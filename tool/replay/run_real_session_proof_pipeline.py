#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.replay.build_real_session_proof_readiness import build_real_session_proof_readiness
from tool.replay.run_real_session_dual_proof_batch import run_real_session_dual_proof_batch


def run_real_session_proof_pipeline(
    *,
    sample_root: Path,
    output_root: Path,
    audit_root: Path,
    replica_strategy_id: str,
    qqq_strategy_id: str,
    qty_tolerance: float,
    min_total_samples: int,
    min_blocked_samples: int,
    days: List[str] | None = None,
) -> Dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    readiness_path = output_root / "real_session_proof_readiness.json"
    readiness = build_real_session_proof_readiness(
        sample_root=sample_root,
        audit_root=audit_root,
        output_path=readiness_path,
        min_total_samples=min_total_samples,
        min_blocked_samples=min_blocked_samples,
    )

    ready_days = [str(row.get("day")) for row in list(readiness.get("rows") or []) if bool(row.get("ready"))]
    selected_days = [day for day in list(days or []) if day in ready_days] if days else ready_days

    report: Dict[str, Any] = {
        "sample_root": str(sample_root),
        "output_root": str(output_root),
        "audit_root": str(audit_root),
        "qqq_strategy_id": qqq_strategy_id,
        "replica_strategy_id": replica_strategy_id,
        "readiness_report_path": str(readiness.get("report_path")),
        "ready_days": ready_days,
        "selected_days": selected_days,
        "pipeline_status": "readiness_only",
        "batch_report_path": None,
        "batch_green": False,
    }

    if selected_days:
        batch = run_real_session_dual_proof_batch(
            sample_root=sample_root,
            output_root=output_root,
            audit_root=audit_root,
            replica_strategy_id=replica_strategy_id,
            qqq_strategy_id=qqq_strategy_id,
            qty_tolerance=qty_tolerance,
            min_total_samples=min_total_samples,
            min_blocked_samples=min_blocked_samples,
            days=selected_days,
        )
        report["pipeline_status"] = "batch_completed"
        report["batch_report_path"] = str(batch.get("report_path"))
        report["batch_green"] = int(batch.get("enablement_attestation_green_days", 0)) == int(batch.get("day_count", 0)) and int(batch.get("day_count", 0)) > 0

    report_path = output_root / "real_session_proof_pipeline.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run readiness + batch proof pipeline for real same-symbol session evidence")
    parser.add_argument("--sample-root", default=str(ROOT / "runtime" / "state" / "index_super_win_shadow_samples"))
    parser.add_argument("--output-root", default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof"))
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--replica-strategy-id", required=True)
    parser.add_argument("--qqq-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    parser.add_argument("--min-total-samples", type=int, default=1)
    parser.add_argument("--min-blocked-samples", type=int, default=1)
    parser.add_argument("--days", nargs="*", default=[])
    args = parser.parse_args()

    report = run_real_session_proof_pipeline(
        sample_root=Path(args.sample_root),
        output_root=Path(args.output_root),
        audit_root=Path(args.audit_root),
        replica_strategy_id=str(args.replica_strategy_id),
        qqq_strategy_id=str(args.qqq_strategy_id),
        qty_tolerance=float(args.qty_tolerance),
        min_total_samples=int(args.min_total_samples),
        min_blocked_samples=int(args.min_blocked_samples),
        days=[str(item) for item in list(args.days or [])],
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("batch_green")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
