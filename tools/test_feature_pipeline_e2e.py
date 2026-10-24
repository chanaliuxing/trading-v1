#!/usr/bin/env python3
from __future__ import annotations
"""
End-to-end regression test for the ML feature pipeline.

Pipeline under test:
  1. DataHealer.ensure_data_ready(symbols)  → bars_1m_ibkr + features_1m
  2. feature_build.py                       → features_1m_candidate view
  3. check_active_completeness.py          → seed features_1m_active from baseline
  4. ml_brain_loop.py (single cycle)       → run exactly one ML job (default: OOF)

The goal is to provide a single script that can be run after feature / DSL /
data pipeline changes to verify that:
  - data self-healing succeeds for the target symbols
  - feature_build can create a candidate view without ClickHouse errors
  - the active table coverage is healthy for the chosen date window
  - the ML Brain loop can run at least one OOF cycle without crashing

Usage (from repo root, with venv):

    source venv/bin/activate
    venv/bin/python tools/test_feature_pipeline_e2e.py \\
        --symbols TSLA NVDA \\
        --date 2025-11-24 \\
        --lookback-days 2
"""

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is on sys.path so that `services.*` and other
# package modules can be imported when this script is executed as
# a standalone CLI (`python tools/test_feature_pipeline_e2e.py`).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end regression test for the ML feature pipeline"
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="Symbols to test (e.g. TSLA NVDA)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Trading date YYYY-MM-DD used for active coverage checks "
             "(default: yesterday)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=2,
        help="Lookback days for DataHealer / backfill (default: 2)",
    )
    parser.add_argument(
        "--start-job-type",
        default="oof",
        choices=["oof", "alpha_loop", "feature_build", "feature_eval"],
        help="Initial ML job type for ml_brain_loop (default: oof)",
    )
    return parser.parse_args()


def _run_subprocess(cmd: List[str], label: str) -> Dict[str, Any]:
    """Run a subprocess and capture basic result information."""
    print(f"\n[STEP] {label}")
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
    return {
        "ok": ok,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _step_data_healer(symbols: List[str], lookback_days: int) -> Dict[str, Any]:
    """Invoke DataHealer.ensure_data_ready for the given symbols."""
    from services.data.data_healer import DataHealer

    runs_dir = Path("runs") / "ai_trader"
    healer = DataHealer(str(runs_dir))
    print(f"\n[STEP] DataHealer.ensure_data_ready symbols={symbols} lookback_days={lookback_days}")
    ok = healer.ensure_data_ready(symbols, lookback_days=lookback_days)
    print(f"  → DataHealer ok={ok}")
    return {"ok": bool(ok)}


def _step_feature_build() -> Dict[str, Any]:
    """Run tools/feature_build.py and parse its JSON status."""
    today_str = date.today().isoformat()
    status_dir = Path("runs") / "ai_trader" / today_str / "features_build"
    status_path = status_dir / "status.json"

    cmd = [sys.executable, "tools/feature_build.py"]
    result = _run_subprocess(cmd, "feature_build (candidate view)")
    summary: Dict[str, Any] = {"ok": result["ok"]}

    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            summary["status_json"] = status
            summary["ok"] = summary["ok"] and bool(status.get("ok", False))
            built = int(status.get("built", 0) or 0)
            print(f"  → feature_build status.json ok={status.get('ok')} built={built}")
        except Exception as exc:
            print(f"  ⚠️ Failed to parse {status_path}: {exc}")
            summary["status_json_error"] = str(exc)
            summary["ok"] = False
    else:
        print(f"  ⚠️ status.json not found at {status_path}")
        summary["ok"] = False

    return summary


def _step_active_seeding(symbols: List[str], date_str: str) -> Dict[str, Any]:
    """Run check_active_completeness.py with auto-seed for the given date/symbols.

    check_active_completeness prints a JSON summary to stdout and uses its
    exit code to encode whether coverage met the threshold. We parse stdout
    instead of expecting it to write an out file.
    """
    sym_arg = ",".join(symbols)

    cmd = [
        sys.executable,
        "tools/check_active_completeness.py",
        "--date",
        date_str,
        "--symbols",
        sym_arg,
        "--min-coverage",
        "0.9",
        "--auto-seed-active",
    ]
    result = _run_subprocess(cmd, "check_active_completeness (auto-seed active)")
    summary: Dict[str, Any] = {"ok": result["ok"]}

    stdout = result.get("stdout") or ""
    if stdout.strip():
        try:
            data = json.loads(stdout)
            summary["coverage"] = data
        except Exception as exc:
            print(f"  ⚠️ Failed to parse JSON from check_active_completeness stdout: {exc}")
            summary["ok"] = False
            summary["parse_error"] = str(exc)
    else:
        summary["ok"] = False

    return summary


def _step_single_ml_cycle(symbols: List[str], start_job_type: str) -> Dict[str, Any]:
    """Run a single ml_brain_loop cycle for the given symbols."""
    cmd = [
        sys.executable,
        "tools/ml_brain_loop.py",
        "--symbols",
        *symbols,
        "--start-job-type",
        start_job_type,
        "--sleep-sec",
        "5",
        "--max-cycles",
        "1",
    ]
    result = _run_subprocess(cmd, "ml_brain_loop (single cycle)")
    return {"ok": result["ok"], "returncode": result["returncode"]}


def main() -> int:
    args = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    if not symbols:
        print("No valid symbols provided.")
        return 1

    if args.date:
        date_str = args.date
    else:
        # Default to yesterday for intraday coverage checks
        date_str = (date.today()).isoformat()

    overall_ok = True
    report: Dict[str, Any] = {
        "symbols": symbols,
        "date": date_str,
        "steps": {},
    }

    # 1) DataHealer
    step = _step_data_healer(symbols, args.lookback_days)
    report["steps"]["data_healer"] = step
    overall_ok = overall_ok and step.get("ok", False)

    # 2) feature_build (candidate view)
    step = _step_feature_build()
    report["steps"]["feature_build"] = step
    overall_ok = overall_ok and step.get("ok", False)

    # 3) active seeding / coverage
    step = _step_active_seeding(symbols, date_str)
    report["steps"]["active_seeding"] = step
    overall_ok = overall_ok and step.get("ok", False)

    # 4) one ML Brain loop cycle (OOF by default)
    step = _step_single_ml_cycle(symbols, args.start_job_type)
    report["steps"]["ml_brain_loop"] = step
    overall_ok = overall_ok and step.get("ok", False)

    print("\n==============================")
    print("Feature pipeline E2E summary")
    print("==============================")
    print(json.dumps(report, indent=2))

    if not overall_ok:
        print("\n❌ One or more steps in the feature pipeline failed.")
        return 1

    print("\n✅ Feature pipeline E2E test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
