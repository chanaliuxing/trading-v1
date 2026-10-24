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

from tool.build_index_super_win_shadow_session_report import build_report as build_shadow_session_report
from tool.replay.build_shadow_session_parity_report import build_shadow_session_parity_report


def run_same_symbol_shadow_proof(
    *,
    sample_dir: Path,
    actual_trace_dir: Path,
    output_dir: Path,
    qty_tolerance: float,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    shadow_session_report = build_shadow_session_report(sample_dir)
    session_report_path = output_dir / "shadow_session_report.json"
    session_report_path.write_text(
        json.dumps(shadow_session_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    parity_report = build_shadow_session_parity_report(
        sample_dir=sample_dir,
        actual_trace_dir=actual_trace_dir,
        output_dir=output_dir / "parity",
        qty_tolerance=qty_tolerance,
    )

    proof = {
        "sample_dir": str(sample_dir),
        "actual_trace_dir": str(actual_trace_dir),
        "output_dir": str(output_dir),
        "shadow_session_report_path": str(session_report_path),
        "shadow_ready_for_cutin": bool(shadow_session_report.get("ready_for_cutin", False)),
        "parity_report_path": str(parity_report.get("report_path")),
        "parity_ready_for_cutin": bool(parity_report.get("ready_for_cutin", False)),
        "same_symbol_proof_green": bool(shadow_session_report.get("ready_for_cutin", False))
        and bool(parity_report.get("ready_for_cutin", False)),
        "sample_count": int(parity_report.get("sample_count", 0)),
        "pass_count": int(parity_report.get("pass_count", 0)),
        "fail_count": int(parity_report.get("fail_count", 0)),
        "missing_actual_count": int(parity_report.get("missing_actual_count", 0)),
    }
    proof_path = output_dir / "same_symbol_shadow_proof.json"
    proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    proof["proof_path"] = str(proof_path)
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description="Run same-symbol shadow proof over one captured session")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--actual-trace-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    args = parser.parse_args()

    proof = run_same_symbol_shadow_proof(
        sample_dir=Path(args.sample_dir),
        actual_trace_dir=Path(args.actual_trace_dir),
        output_dir=Path(args.output_dir),
        qty_tolerance=float(args.qty_tolerance),
    )
    print(json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(proof.get("same_symbol_proof_green")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
