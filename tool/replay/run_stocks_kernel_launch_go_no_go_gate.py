#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.report.build_stocks_kernel_launch_go_no_go_gate import build_stocks_kernel_launch_go_no_go_gate


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run stocks kernel launch GO/BLOCK gate")
    parser.add_argument("--readiness-report", default=str(ROOT / "runtime" / "state" / "stocks_kernel_readiness.json"))
    parser.add_argument("--matrix-report", default=str(ROOT / "runtime" / "state" / "stocks_kernel_regression_matrix.json"))
    parser.add_argument("--audit-summary", default=str(ROOT / "runtime" / "state" / "stocks_kernel_audit_summary.json"))
    parser.add_argument("--output", default=str(ROOT / "runtime" / "state" / "stocks_kernel_launch_go_no_go_gate.json"))
    args = parser.parse_args()

    report = build_stocks_kernel_launch_go_no_go_gate(
        readiness_report=_load_json(Path(args.readiness_report).expanduser()),
        matrix_report=_load_json(Path(args.matrix_report).expanduser()),
        audit_summary=_load_json(Path(args.audit_summary).expanduser()),
    )
    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report_path": str(out), "recommendation": report.get("recommendation")}, ensure_ascii=False))
    return 0 if str(report.get("recommendation") or "") == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
