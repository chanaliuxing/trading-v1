#!/usr/bin/env python3
from __future__ import annotations
"""
Test helper: inspect data-quality state for a given date & symbols.

It wraps tools/check_active_completeness.py to show:
  - bars count
  - active count
  - coverage
and highlights symbols that cause data_quality_check to fail.

Usage:
  venv/bin/python tools/test_data_quality_state.py --date 2025-11-22 --symbols TSLA,AAPL
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="Trading date YYYY-MM-DD")
    ap.add_argument("--symbols", required=False, help="Comma-separated symbols (optional; default=from active table)")
    ap.add_argument("--min-coverage", type=float, default=0.98, help="Coverage threshold (same as data_quality_check)")
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    cmd = [
        sys.executable,
        "tools/check_active_completeness.py",
        "--date",
        args.date,
        "--min-coverage",
        str(args.min_coverage),
    ]
    if args.symbols:
        cmd.extend(["--symbols", args.symbols])

    print(f"Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if not proc.stdout:
        print(f"[ERROR] No output from check_active_completeness.py", file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(1)

    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        print(f"[ERROR] Failed to parse JSON: {exc}", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        raise SystemExit(1)

    date_str = payload.get("date", args.date)
    print(f"\n=== Data quality for {date_str} ===")
    print(f"Window: {payload.get('start')} ~ {payload.get('end')}")
    print(f"Min coverage threshold: {payload.get('min_coverage')}")
    print()

    symbols = payload.get("symbols") or []
    if not symbols:
        print("No symbols reported.")
        return

    bad_syms = []
    for rec in symbols:
        sym = rec.get("symbol")
        bars = rec.get("bars")
        active = rec.get("active")
        cov = rec.get("coverage")
        print(f"- {sym}: bars={bars}, active={active}, coverage={cov:.4f}")
        if cov < args.min_coverage:
            bad_syms.append(sym)

    print()
    if bad_syms:
        print(f"⚠️  Symbols below coverage threshold ({args.min_coverage}): {', '.join(bad_syms)}")
        rc = 1
    else:
        print("✅ All symbols meet coverage threshold.")
        rc = 0

    # Mirror data_quality_check semantics: RC=0 if healthy, 1 if low coverage
    raise SystemExit(rc)


if __name__ == "__main__":
    main()

