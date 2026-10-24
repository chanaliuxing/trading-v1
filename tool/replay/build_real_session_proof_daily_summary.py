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


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_real_session_proof_daily_summary(*, output_root: Path) -> Dict[str, Any]:
    readiness = _load_json(output_root / "real_session_proof_readiness.json")
    pipeline = _load_json(output_root / "real_session_proof_pipeline.json")
    batch = _load_json(output_root / "real_session_dual_proof_batch_report.json")

    readiness_rows = list(readiness.get("rows") or [])
    batch_rows = list(batch.get("rows") or [])
    ready_days = [str(row.get("day")) for row in readiness_rows if bool(row.get("ready"))]
    blocked_by_reason: Dict[str, int] = {}
    for row in readiness_rows:
        for reason in list(row.get("reasons") or []):
            blocked_by_reason[str(reason)] = blocked_by_reason.get(str(reason), 0) + 1

    non_green_days: List[Dict[str, Any]] = []
    ownership_missing_days: List[Dict[str, Any]] = []
    ownership_missing_sample_total = 0
    for row in batch_rows:
        missing_ownership = int(row.get("samples_missing_ownership_evidence", 0) or 0)
        ownership_missing_sample_total += max(0, missing_ownership)
        if missing_ownership > 0:
            ownership_missing_days.append(
                {
                    "day": row.get("day"),
                    "samples_missing_ownership_evidence": missing_ownership,
                    "ownership_evidence_green": bool(row.get("ownership_evidence_green", False)),
                }
            )
        if bool(row.get("enablement_attestation_green")):
            continue
        non_green_days.append(
            {
                "day": row.get("day"),
                "status": row.get("status"),
                "overall_green": row.get("overall_green"),
                "enablement_attestation_green": row.get("enablement_attestation_green"),
                "ownership_evidence_green": row.get("ownership_evidence_green"),
            }
        )

    summary = {
        "output_root": str(output_root),
        "pipeline_status": pipeline.get("pipeline_status"),
        "has_any_ready_day": bool(readiness.get("has_any_ready_day", False)),
        "ready_day_count": int(readiness.get("ready_day_count", 0) or 0),
        "ready_days": ready_days,
        "blocked_day_reason_counter": blocked_by_reason,
        "batch_day_count": int(batch.get("day_count", 0) or 0),
        "batch_green_days": int(batch.get("green_days", 0) or 0),
        "batch_enablement_attestation_green_days": int(batch.get("enablement_attestation_green_days", 0) or 0),
        "ownership_missing_sample_total": ownership_missing_sample_total,
        "ownership_missing_days": ownership_missing_days,
        "non_green_days": non_green_days,
        "needs_more_real_samples": not bool(readiness.get("has_any_ready_day", False)),
    }
    summary_path = output_root / "real_session_proof_daily_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["report_path"] = str(summary_path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build operator summary for real-session proof pipeline outputs")
    parser.add_argument("--output-root", default=str(ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof"))
    args = parser.parse_args()

    summary = build_real_session_proof_daily_summary(output_root=Path(args.output_root))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
