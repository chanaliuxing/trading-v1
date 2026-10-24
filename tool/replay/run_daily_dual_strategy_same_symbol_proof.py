#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tool.replay.run_dual_strategy_same_symbol_proof import run_dual_strategy_same_symbol_proof

ET = ZoneInfo("America/New_York")


def _default_sample_dir(day: str) -> Path:
    return ROOT / "runtime" / "state" / "index_super_win_shadow_samples" / day


def _default_output_dir(day: str) -> Path:
    return ROOT / "runtime" / "state" / "index_super_win_same_symbol_proof" / day


def main() -> int:
    parser = argparse.ArgumentParser(description="Run daily dual-strategy same-symbol proof using default session directories")
    parser.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--sample-dir", default="")
    parser.add_argument("--audit-root", default=str(ROOT / "runtime" / "audit"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--qqq-strategy-id", default="qqq_intraday_001")
    parser.add_argument("--replica-strategy-id", required=True)
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    parser.add_argument("--min-total-samples", type=int, default=1)
    parser.add_argument("--min-blocked-samples", type=int, default=1)
    args = parser.parse_args()

    day = str(args.day)
    sample_dir = Path(args.sample_dir) if str(args.sample_dir).strip() else _default_sample_dir(day)
    output_dir = Path(args.output_dir) if str(args.output_dir).strip() else _default_output_dir(day)
    report = run_dual_strategy_same_symbol_proof(
        sample_dir=sample_dir,
        audit_root=Path(args.audit_root),
        output_dir=output_dir,
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
