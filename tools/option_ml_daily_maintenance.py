"""
tools/option_ml_daily_maintenance.py

Daily maintenance runner:
- perf summary
- model meta update
- coverage curve
- risk cap validation
- degradation guard

Usage:
  venv/bin/python tools/option_ml_daily_maintenance.py \
    --start 2025-12-01 --end 2026-01-01 \
    --model-version options_long_v1_20260106_024626 \
    --structure-model-version options_struct_v1_20260106_032153 \
    --equity 100000 \
    --backfill-only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--structure-model-version", required=True)
    parser.add_argument("--equity", type=float, default=100000.0)
    parser.add_argument("--backfill-only", action="store_true")
    args = parser.parse_args()

    python = sys.executable
    backfill_flag = ["--backfill-only"] if args.backfill_only else []

    _run([python, "tools/option_ml_perf_summary.py", "--start", args.start, "--end", args.end, *backfill_flag])
    _run([python, "tools/update_option_ml_models_perf.py", "--start", args.start, "--end", args.end, *backfill_flag])
    _run([
        python,
        "tools/option_ml_coverage_curve.py",
        "--start",
        args.start,
        "--end",
        args.end,
        "--model-version",
        args.model_version,
    ])
    _run([
        python,
        "tools/validate_option_ml_risk_caps.py",
        "--start",
        args.start,
        "--end",
        args.end,
        "--equity",
        str(args.equity),
        *backfill_flag,
    ])
    _run([python, "tools/option_ml_degradation_guard.py", "--equity", str(args.equity)])


if __name__ == "__main__":
    main()
