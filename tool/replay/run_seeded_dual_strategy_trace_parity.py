#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.replay.build_qqq_trace_parity_samples import build_trace_parity_samples
from tool.replay.build_dual_strategy_trace_parity_report import build_dual_strategy_trace_parity_report


def _default_sample_dir(day: str) -> Path:
    return ROOT / "runtime" / "state" / "index_super_win_shadow_samples" / day / "qqq_trace_seeded"


def _default_output_dir(day: str) -> Path:
    return ROOT / "runtime" / "state" / "index_super_win_trace_parity" / day / "qqq_trace_seeded"


def run_seeded_dual_strategy_trace_parity(
    *,
    day: str,
    audit_root: Path,
    sample_dir: Path,
    output_dir: Path,
    baseline_strategy_id: str,
    baseline_summary_prefix: str,
    baseline_source_type: str,
    underlier_aliases: dict[str, str] | None,
    qqq_strategy_id: str,
    replica_strategy_id: str,
    qty_tolerance: float,
) -> dict:
    seed_manifest = build_trace_parity_samples(
        audit_root=audit_root,
        day=day,
        output_dir=sample_dir,
        strategy_id=baseline_strategy_id,
        summary_prefix=baseline_summary_prefix,
        source_type=baseline_source_type,
        underlier_aliases=underlier_aliases,
    )
    parity_report = build_dual_strategy_trace_parity_report(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=output_dir,
        qqq_strategy_id=qqq_strategy_id,
        replica_strategy_id=replica_strategy_id,
        qty_tolerance=qty_tolerance,
    )
    report = {
        "day": day,
        "audit_root": str(audit_root),
        "sample_dir": str(sample_dir),
        "output_dir": str(output_dir),
        "baseline_strategy_id": baseline_strategy_id,
        "baseline_summary_prefix": baseline_summary_prefix,
        "seed_manifest_path": str(seed_manifest.get("manifest_path") or ""),
        "seed_sample_count": int(seed_manifest.get("sample_count", 0)),
        "parity_report_path": str(parity_report.get("report_path") or ""),
        "parity_sample_count": int(parity_report.get("sample_count", 0)),
        "pass_count": int(parity_report.get("pass_count", 0)),
        "fail_count": int(parity_report.get("fail_count", 0)),
        "missing_count": int(parity_report.get("missing_count", 0)),
        "ready_for_parity": bool(parity_report.get("ready_for_parity", False)),
    }
    report_path = output_dir / "seeded_dual_strategy_trace_parity.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed parity samples from qqq trace audit and run dual-strategy trace parity")
    parser.add_argument("--day", required=True)
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--sample-dir", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--baseline-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--baseline-summary-prefix", default="qqq")
    parser.add_argument("--baseline-source-type", default="qqq_trace_audit")
    parser.add_argument("--qqq-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--replica-strategy-id", default="index_super_win_001")
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    args = parser.parse_args()

    day = str(args.day)
    sample_dir = Path(args.sample_dir) if str(args.sample_dir).strip() else _default_sample_dir(day)
    output_dir = Path(args.output_dir) if str(args.output_dir).strip() else _default_output_dir(day)
    report = run_seeded_dual_strategy_trace_parity(
        day=day,
        audit_root=Path(args.audit_root),
        sample_dir=sample_dir,
        output_dir=output_dir,
        baseline_strategy_id=str(args.baseline_strategy_id),
        baseline_summary_prefix=str(args.baseline_summary_prefix),
        baseline_source_type=str(args.baseline_source_type),
        underlier_aliases={"TQQQ": "QQQ", "SQQQ": "QQQ"} if str(args.baseline_summary_prefix).strip().lower() == "qqq" else None,
        qqq_strategy_id=str(args.qqq_strategy_id),
        replica_strategy_id=str(args.replica_strategy_id),
        qty_tolerance=float(args.qty_tolerance),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("ready_for_parity")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
