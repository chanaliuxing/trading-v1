#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.replay.run_daily_dual_strategy_same_symbol_proof import _default_output_dir, _default_sample_dir
from tool.replay.run_dual_strategy_same_symbol_proof import run_dual_strategy_same_symbol_proof


def _iter_day_dirs(sample_root: Path) -> Iterable[Path]:
    if not sample_root.exists():
        return []
    return sorted([path for path in sample_root.iterdir() if path.is_dir() and _looks_like_day(path.name)], key=lambda p: p.name)


def _looks_like_day(raw: str) -> bool:
    try:
        datetime.strptime(str(raw), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _sample_json_count(sample_dir: Path) -> int:
    if not sample_dir.exists():
        return 0
    return len([p for p in sample_dir.glob("*.json") if p.name not in {"session_report.json", "session_manifest.json"}])


def run_real_session_dual_proof_batch(
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
) -> Dict[str, object]:
    selected_days = list(days or [])
    if selected_days:
        day_dirs = [sample_root / day for day in selected_days if _looks_like_day(day)]
    else:
        day_dirs = list(_iter_day_dirs(sample_root))

    rows = []
    green_days = 0
    attestation_green_days = 0
    total_samples = 0

    for day_dir in day_dirs:
        day = day_dir.name
        sample_count = _sample_json_count(day_dir)
        row = {
            "day": day,
            "sample_dir": str(day_dir),
            "sample_count": sample_count,
        }
        if sample_count <= 0:
            row.update(
                {
                    "status": "skipped_no_samples",
                    "overall_green": False,
                    "enablement_attestation_green": False,
                }
            )
            rows.append(row)
            continue

        out_dir = output_root / day
        proof = run_dual_strategy_same_symbol_proof(
            sample_dir=day_dir,
            audit_root=audit_root,
            output_dir=out_dir,
            qqq_strategy_id=qqq_strategy_id,
            replica_strategy_id=replica_strategy_id,
            qty_tolerance=qty_tolerance,
            min_total_samples=min_total_samples,
            min_blocked_samples=min_blocked_samples,
        )
        row.update(
            {
                "status": "completed",
                "report_path": str(proof.get("report_path")),
                "overall_green": bool(proof.get("overall_green", False)),
                "enablement_attestation_green": bool(proof.get("enablement_attestation_green", False)),
                "dual_export_green": bool(proof.get("dual_export_green", False)),
                "qqq_expectation_ok": bool(proof.get("qqq_expectation_ok", False)),
                "summary_compare_green": bool(proof.get("summary_compare_green", False)),
                "foreign_safety_green": bool(proof.get("foreign_safety_green", False)),
                "ownership_evidence_green": bool(proof.get("ownership_evidence_green", False)),
                "samples_missing_ownership_evidence": int(proof.get("samples_missing_ownership_evidence", 0) or 0),
            }
        )
        if row["overall_green"]:
            green_days += 1
        if row["enablement_attestation_green"]:
            attestation_green_days += 1
        total_samples += sample_count
        rows.append(row)

    report = {
        "sample_root": str(sample_root),
        "output_root": str(output_root),
        "audit_root": str(audit_root),
        "qqq_strategy_id": qqq_strategy_id,
        "replica_strategy_id": replica_strategy_id,
        "days_requested": selected_days,
        "day_count": len(rows),
        "green_days": green_days,
        "enablement_attestation_green_days": attestation_green_days,
        "total_samples": total_samples,
        "rows": rows,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "real_session_dual_proof_batch_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run batch dual-strategy same-symbol proof over real dated session directories")
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

    report = run_real_session_dual_proof_batch(
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
    return 0 if int(report.get("enablement_attestation_green_days", 0)) == int(report.get("day_count", 0)) and int(report.get("day_count", 0)) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
