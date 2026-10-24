#!/usr/bin/env python3
from __future__ import annotations
"""
Feature pipeline healthcheck wrapper.

This script runs the core validation tools for the feature pipeline:

  1. tools/test_feature_catalog_sql.py
     - Static check: all f./m5./d. column references in config/features_catalog.json
       exist in ClickHouse base tables.

  2. tools/validate_dsl_features.py
     - Dynamic check: compile DSL → SQL for catalog specs and dry-run expressions
       against ClickHouse; reports compile/dry-run errors and all-null/constant
       features. Writes a JSON report under runs/tests/.

  3. tools/test_active_coverage_consistency.py (optional but recommended)
     - For a given date/symbol set, checks coverage of:
         trading.bars_1m_ibkr vs trading.features_1m vs trading.features_1m_active
       to detect underfilled active tables.

Usage (from repo root, with venv):

    source venv/bin/activate
    venv/bin/python tools/feature_pipeline_healthcheck.py \\
        --symbols TSLA,SPY,NVDA \\
        --date 2025-11-24
"""

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Feature pipeline healthcheck (catalog + DSL + active coverage)"
    )
    parser.add_argument(
        "--symbols",
        default="TSLA,NVDA",
        help="Comma-separated symbols for active coverage checks (default: TSLA,NVDA)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Trading date YYYY-MM-DD for active coverage checks "
             "(default: yesterday)",
    )
    parser.add_argument(
        "--sample-symbol",
        default="TSLA",
        help="Sample symbol for DSL dry-run (default: TSLA)",
    )
    return parser.parse_args()


def _run_subprocess(cmd: List[str], label: str) -> Dict[str, Any]:
    print(f"\n[CHECK] {label}")
    print("  CMD:", " ".join(cmd))
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  ❌ Subprocess failed to start: {exc}")
        return {"ok": False, "error": str(exc), "returncode": None}

    if completed.stdout:
        print("  STDOUT:")
        for line in completed.stdout.splitlines():
            print("   ", line)
    if completed.stderr:
        print("  STDERR:")
        for line in completed.stderr.splitlines():
            print("   ", line)

    ok = completed.returncode == 0
    print(f"  → exit code={completed.returncode} ({'OK' if ok else 'FAIL'})")
    return {"ok": ok, "returncode": completed.returncode}


def main() -> int:
    args = _parse_args()

    if args.date:
        date_str = args.date
    else:
        # Default to yesterday to avoid partial intraday sessions
        date_str = (date.today() - timedelta(days=1)).isoformat()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    sample_symbol = args.sample_symbol.strip().upper() or "TSLA"

    overall_ok = True
    summary: Dict[str, Any] = {
        "date": date_str,
        "symbols": symbols,
        "sample_symbol": sample_symbol,
        "checks": {},
    }

    # 1) Catalog SQL references
    step = _run_subprocess(
        [sys.executable, "tools/test_feature_catalog_sql.py"],
        "test_feature_catalog_sql (static column references)",
    )
    summary["checks"]["catalog_sql"] = step
    overall_ok = overall_ok and step.get("ok", False)

    # 2) DSL compile + dry-run
    dsl_out = Path("runs") / "tests" / "dsl_catalog_validation.json"
    dsl_out.parent.mkdir(parents=True, exist_ok=True)
    step = _run_subprocess(
        [
            sys.executable,
            "tools/validate_dsl_features.py",
            "--sample-symbol",
            sample_symbol,
            "--limit",
            "10",
            "--out",
            str(dsl_out),
        ],
        "validate_dsl_features (compile + dry-run)",
    )
    step["out_json"] = str(dsl_out)
    summary["checks"]["dsl_validate"] = step
    overall_ok = overall_ok and step.get("ok", False)

    # 3) Active coverage consistency (soft requirement; we still expose status)
    cov_out = Path("runs") / "tests" / f"active_coverage_{date_str}.json"
    cov_out.parent.mkdir(parents=True, exist_ok=True)
    cov_step = _run_subprocess(
        [
            sys.executable,
            "tools/test_active_coverage_consistency.py",
            "--symbols",
            ",".join(symbols),
            "--date",
            date_str,
            "--min-bars",
            "300",
            "--out",
            str(cov_out),
        ],
        "test_active_coverage_consistency (features vs active)",
    )
    cov_step["out_json"] = str(cov_out)
    summary["checks"]["active_coverage"] = cov_step
    # active coverage check is allowed to fail without forcing overall_ok=False,
    # because in some environments bars_1m_ibkr may legitimately be incomplete
    # for the chosen date. We still propagate the exit code in the summary so
    # operators and CI can make their own decision.

    print("\n==============================")
    print("Feature pipeline healthcheck")
    print("==============================")
    print(json.dumps(summary, indent=2))

    if not overall_ok:
        print("\n❌ One or more core checks failed (catalog_sql / dsl_validate).")
        return 1

    print("\n✅ Core feature pipeline healthchecks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

