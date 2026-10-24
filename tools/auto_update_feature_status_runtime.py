#!/usr/bin/env python3
from __future__ import annotations
"""
Auto-update runtime feature status based on coverage in features_1m_active.

This script is intended to be called by host services (e.g., DataHealer or
nightly jobs) to automatically mark columns as enabled/disabled at runtime
based on their non-null coverage over a recent window.

It uses tools/check_feature_column_coverage.py to compute coverage and writes
the result into runs/ai_trader/<today>/feature_status.runtime.json so that
control_event_consumer can dynamically forbid columns with poor coverage.

Usage:
    source venv/bin/activate
    venv/bin/python tools/auto_update_feature_status_runtime.py \\
        --symbols TSLA,SPY,QQQ \\
        --days 5
"""

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Auto-update runtime feature_status based on active table coverage"
    )
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols (e.g., TSLA,SPY,NVDA)",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=5,
        help="Lookback days for coverage analysis (default: 5)",
    )
    ap.add_argument(
        "--table",
        default="trading.features_1m_active",
        help="Table to analyze (default: trading.features_1m_active)",
    )
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("No valid symbols provided.", file=sys.stderr)
        return 1

    # Delegate coverage computation to existing CLI tool
    cmd = [
        sys.executable,
        "tools/check_feature_column_coverage.py",
        "--symbols",
        ",".join(symbols),
        "--days",
        str(int(args.days)),
        "--table",
        args.table,
    ]
    print("[auto_feature_status] Running:", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as exc:
        print(f"[auto_feature_status] Failed to run coverage tool: {exc}", file=sys.stderr)
        return 1

    if not proc.stdout:
        print("[auto_feature_status] Coverage tool produced no output", file=sys.stderr)
        return 1

    try:
        coverage_data: List[Dict[str, Any]] = json.loads(proc.stdout)
    except Exception as exc:
        print(f"[auto_feature_status] Failed to parse coverage JSON: {exc}", file=sys.stderr)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        return 1

    # Build runtime status: high coverage columns -> enabled_auto, low coverage -> disabled_auto
    status: Dict[str, Any] = {"columns": {}}
    # Simple threshold; can be tuned over time.
    enable_threshold = 0.9

    for item in coverage_data:
        col = item.get("column")
        ratio = float(item.get("non_null_ratio", 0.0) or 0.0)
        if not col:
            continue
        if ratio >= enable_threshold:
            status["columns"][col] = "enabled_auto"
        else:
            status["columns"][col] = "disabled_auto_low_coverage"

    # Persist under today's run directory so Brain/validators can merge it.
    today = date.today().isoformat()
    out_dir = Path("runs") / "ai_trader" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "feature_status.runtime.json"
    out_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(f"[auto_feature_status] Runtime status written to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

