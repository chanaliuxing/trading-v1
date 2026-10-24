#!/usr/bin/env python3
"""Run the legacy best-performing max3 + gateA backtest with frozen parameters."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN = REPO_ROOT / "scripts/backtest_new_filter_main.py"
FROZEN_SEED = REPO_ROOT / "runtime/state/snapshots/qqq_intraday_atr_seed_2026-02-23_freeze.json"
OUT_DIR = REPO_ROOT / "junk/backtest_results/latest"


def main() -> None:
    ap = argparse.ArgumentParser(description="Frozen legacy max3+gateA runner")
    ap.add_argument("--start-date", default="2025-08-01")
    ap.add_argument("--end-date", default="2026-02-24")
    ap.add_argument("--tag", default="legacy_frozen")
    ap.add_argument("--report-file", default="")
    ap.add_argument("--csv-file", default="")
    args = ap.parse_args()

    if not MAIN.exists():
        raise SystemExit(f"missing backtest script: {MAIN}")
    if not FROZEN_SEED.exists():
        raise SystemExit(f"missing frozen seed file: {FROZEN_SEED}")

    report_file = args.report_file or str(
        OUT_DIR / f"new_filter_daily_report_{args.start_date}_{args.end_date}_pure_drift_max3_gateA_{args.tag}.md"
    )
    csv_file = args.csv_file or str(
        OUT_DIR / f"new_filter_daily_report_{args.start_date}_{args.end_date}_pure_drift_max3_gateA_{args.tag}.csv"
    )

    cmd = [
        sys.executable,
        str(MAIN),
        "--start-date",
        args.start_date,
        "--end-date",
        args.end_date,
        "--target-atr",
        "1.0",
        "--hard-stop-atr",
        "0.8",
        "--logic-retreat-min",
        "7",
        "--notional-usd",
        "10000",
        "--max-trades-per-day",
        "3",
        "--third-trade-require-first2-positive",
        "--chop-mode",
        "skip",
        "--atr-rank-chop-max",
        "0.15",
        "--delayed-confirm-move5m-min",
        "0.155",
        "--fast-vol-min",
        "999",
        "--early-lock-mode",
        "openclose",
        "--atr-seed-path",
        str(FROZEN_SEED),
        "--report-file",
        report_file,
        "--csv-file",
        csv_file,
    ]

    print(" ".join(cmd))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
