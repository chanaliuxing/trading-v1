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

from tool.replay.build_dual_strategy_daily_summary_compare import build_dual_strategy_daily_summary_compare
from tool.replay.build_dual_strategy_enablement_attestation import build_dual_strategy_enablement_attestation
from tool.replay.build_dual_strategy_foreign_safety_report import build_dual_strategy_foreign_safety_report
from tool.replay.export_strategy_traces_for_shadow_session import export_strategy_traces_for_shadow_session
from tool.replay.run_same_symbol_shadow_proof import run_same_symbol_shadow_proof


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_qqq_expectation_report(sample_dir: Path, qqq_export: Dict[str, Any]) -> Dict[str, Any]:
    exported_by_sample = {
        str(item.get("sample_path")): item
        for item in list(qqq_export.get("rows") or [])
        if str(item.get("sample_path") or "").strip()
    }
    rows = []
    ok_count = 0
    mismatch_count = 0
    sample_paths = sorted(
        [path for path in sample_dir.glob("*.json") if path.name not in {"session_report.json", "session_manifest.json"}],
        key=lambda p: p.name,
    )
    for sample_path in sample_paths:
        sample = _load_json(sample_path)
        summary = dict(sample.get("summary") or {})
        symbol = str(sample.get("symbol") or "").strip().upper()
        expects_qqq_trace = bool(summary.get("qqq_is_traded", False)) and (
            str(summary.get("qqq_exec_symbol") or "").strip().upper() == symbol
        )
        export_row = exported_by_sample.get(str(sample_path), {})
        exported = str(export_row.get("status") or "") == "exported"
        row = {
            "sample_path": str(sample_path),
            "symbol": symbol,
            "expects_qqq_trace": expects_qqq_trace,
            "qqq_export_status": export_row.get("status"),
            "qqq_trace_id": export_row.get("trace_id"),
        }
        if expects_qqq_trace == exported:
            row["status"] = "ok"
            ok_count += 1
        else:
            row["status"] = "mismatch"
            mismatch_count += 1
        rows.append(row)
    return {
        "sample_count": len(rows),
        "ok_count": ok_count,
        "mismatch_count": mismatch_count,
        "ok": mismatch_count == 0,
        "rows": rows,
    }


def run_dual_strategy_same_symbol_proof(
    *,
    sample_dir: Path,
    audit_root: Path,
    output_dir: Path,
    qqq_strategy_id: str,
    replica_strategy_id: str,
    qty_tolerance: float,
    min_total_samples: int,
    min_blocked_samples: int,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    qqq_trace_dir = output_dir / "qqq_actual_traces"
    replica_trace_dir = output_dir / "replica_actual_traces"

    qqq_export = export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=qqq_trace_dir,
        strategy_id=qqq_strategy_id,
    )
    replica_export = export_strategy_traces_for_shadow_session(
        sample_dir=sample_dir,
        audit_root=audit_root,
        output_dir=replica_trace_dir,
        strategy_id=replica_strategy_id,
    )
    proof = run_same_symbol_shadow_proof(
        sample_dir=sample_dir,
        actual_trace_dir=replica_trace_dir,
        output_dir=output_dir / "replica_same_symbol_proof",
        qty_tolerance=qty_tolerance,
    )
    qqq_expectation = _build_qqq_expectation_report(sample_dir, qqq_export)
    summary_compare = build_dual_strategy_daily_summary_compare(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_trace_dir,
        replica_trace_dir=replica_trace_dir,
        output_dir=output_dir,
    )
    foreign_safety = build_dual_strategy_foreign_safety_report(
        sample_dir=sample_dir,
        qqq_trace_dir=qqq_trace_dir,
        replica_trace_dir=replica_trace_dir,
        audit_root=audit_root,
        output_dir=output_dir,
    )

    report = {
        "sample_dir": str(sample_dir),
        "audit_root": str(audit_root),
        "output_dir": str(output_dir),
        "qqq_strategy_id": qqq_strategy_id,
        "replica_strategy_id": replica_strategy_id,
        "qqq_export_report_path": str(qqq_export.get("report_path")),
        "qqq_exported_count": int(qqq_export.get("exported_count", 0)),
        "qqq_missing_count": int(qqq_export.get("missing_count", 0)),
        "replica_export_report_path": str(replica_export.get("report_path")),
        "replica_exported_count": int(replica_export.get("exported_count", 0)),
        "replica_missing_count": int(replica_export.get("missing_count", 0)),
        "qqq_expectation_ok": bool(qqq_expectation.get("ok", False)),
        "qqq_expectation_report": qqq_expectation,
        "replica_proof_path": str(proof.get("proof_path")),
        "replica_same_symbol_proof_green": bool(proof.get("same_symbol_proof_green", False)),
        "summary_compare_path": str(summary_compare.get("report_path")),
        "summary_compare_green": bool(summary_compare.get("summary_compare_green", False)),
        "foreign_safety_report_path": str(foreign_safety.get("report_path")),
        "foreign_safety_green": bool(foreign_safety.get("foreign_safety_green", False)),
        "ownership_evidence_green": int(foreign_safety.get("samples_missing_ownership_evidence", 0) or 0) == 0,
        "samples_missing_ownership_evidence": int(foreign_safety.get("samples_missing_ownership_evidence", 0) or 0),
        "dual_export_green": int(qqq_export.get("missing_count", 0)) == 0 and int(replica_export.get("missing_count", 0)) == 0,
    }
    report["overall_green"] = (
        bool(report["dual_export_green"])
        and bool(report["replica_same_symbol_proof_green"])
        and bool(report["qqq_expectation_ok"])
        and bool(report["summary_compare_green"])
        and bool(report["foreign_safety_green"])
    )
    report_path = output_dir / "dual_strategy_same_symbol_proof.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    attestation = build_dual_strategy_enablement_attestation(
        dual_proof_report_path=report_path,
        output_dir=output_dir,
        min_total_samples=int(min_total_samples),
        min_blocked_samples=int(min_blocked_samples),
    )
    report["enablement_attestation_path"] = str(attestation.get("report_path"))
    report["enablement_attestation_green"] = bool(attestation.get("attestation_green", False))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dual-strategy same-symbol proof for qqq and replica")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--qqq-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--replica-strategy-id", required=True)
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    parser.add_argument("--min-total-samples", type=int, default=1)
    parser.add_argument("--min-blocked-samples", type=int, default=1)
    args = parser.parse_args()

    report = run_dual_strategy_same_symbol_proof(
        sample_dir=Path(args.sample_dir),
        audit_root=Path(args.audit_root),
        output_dir=Path(args.output_dir),
        qqq_strategy_id=str(args.qqq_strategy_id),
        replica_strategy_id=str(args.replica_strategy_id),
        qty_tolerance=float(args.qty_tolerance),
        min_total_samples=int(args.min_total_samples),
        min_blocked_samples=int(args.min_blocked_samples),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(report.get("overall_green")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
