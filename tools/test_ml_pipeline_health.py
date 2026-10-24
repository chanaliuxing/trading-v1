#!/usr/bin/env python3
"""
ML Pipeline Health Check - Integration Test

Runs all health checks and produces a comprehensive report:
1. Raw bars health
2. Feature columns health
3. Label health
4. Active coverage consistency

Usage:
    venv/bin/python tools/test_ml_pipeline_health.py \
        --symbols TSLA,NVDA,SPY \
        --start 2025-11-20 \
        --end   2025-11-24 \
        --out-dir runs/tests/pipeline_health_2025-11-24
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="ML Pipeline Health Check"
    )
    ap.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols"
    )
    ap.add_argument(
        "--start",
        required=True,
        help="Start date (YYYY-MM-DD)"
    )
    ap.add_argument(
        "--end",
        required=True,
        help="End date (YYYY-MM-DD)"
    )
    ap.add_argument(
        "--feature-cols",
        default="ret_1,ret_5,atr_14,vwap,rsi_14",
        help="Feature columns to test (default: ret_1,ret_5,atr_14,vwap,rsi_14)"
    )
    ap.add_argument(
        "--label-cols",
        default="",
        help="Label columns to test (optional, e.g., h5_bps10,h10_bps7)"
    )
    ap.add_argument(
        "--out-dir",
        required=True,
        help="Output directory for all results"
    )
    return ap.parse_args()


def run_script(script_path: str, args: List[str], out_file: Path) -> Dict[str, Any]:
    """
    Run a test script and capture results.

    Returns:
        {"ok": bool, "exit_code": int, "output_file": str}
    """
    cmd = ["venv/bin/python", script_path] + args + ["--out", str(out_file)]

    print(f"  Running: {Path(script_path).name}...")
    print(f"    Command: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "output_file": str(out_file)
        }
    except subprocess.TimeoutExpired:
        print(f"  ❌ Timeout after 5 minutes")
        return {
            "ok": False,
            "exit_code": -1,
            "error": "Timeout"
        }
    except Exception as e:
        print(f"  ❌ Exception: {e}")
        return {
            "ok": False,
            "exit_code": -1,
            "error": str(e)
        }


def load_json_results(file_path: Path) -> List[Dict[str, Any]]:
    """Load JSON results from a file."""
    if not file_path.exists():
        return []

    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def main():
    args = parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("ML Pipeline Health Check")
    print("=" * 80)
    print(f"Symbols: {args.symbols}")
    print(f"Period: {args.start} to {args.end}")
    print(f"Output directory: {out_dir}")
    print()

    results: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "symbols": args.symbols.split(","),
        "start": args.start,
        "end": args.end,
        "checks": {}
    }

    # 1. Test raw bars health
    print("[1/4] Testing raw bars health...")
    bars_result = run_script(
        "tools/test_raw_bars_health.py",
        [
            "--symbols", args.symbols,
            "--start", args.start,
            "--end", args.end
        ],
        out_dir / "raw_bars_health.json"
    )
    results["checks"]["raw_bars"] = bars_result

    # 2. Test feature columns health
    print("\n[2/4] Testing feature columns health...")
    features_result = run_script(
        "tools/test_feature_columns_health.py",
        [
            "--symbols", args.symbols,
            "--feature-cols", args.feature_cols,
            "--start", args.start,
            "--end", args.end
        ],
        out_dir / "feature_columns_health.json"
    )
    results["checks"]["features"] = features_result

    # 3. Test label health (optional)
    if args.label_cols:
        print("\n[3/4] Testing label health...")
        labels_result = run_script(
            "tools/test_label_health.py",
            [
                "--symbols", args.symbols,
                "--label-cols", args.label_cols,
                "--start", args.start,
                "--end", args.end
            ],
            out_dir / "label_health.json"
        )
        results["checks"]["labels"] = labels_result
    else:
        print("\n[3/4] Skipping label health (no --label-cols provided)")
        results["checks"]["labels"] = {"ok": True, "skipped": True}

    # 4. Test active coverage consistency (latest day only)
    print("\n[4/4] Testing active coverage consistency...")
    active_result = run_script(
        "tools/test_active_coverage_consistency.py",
        [
            "--symbols", args.symbols,
            "--date", args.end
        ],
        out_dir / "active_coverage.json"
    )
    results["checks"]["active"] = active_result

    # Generate summary
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)

    summary = {
        "raw_bars_ok": bars_result.get("ok", False),
        "features_ok": features_result.get("ok", False),
        "labels_ok": results["checks"]["labels"].get("ok", True),  # Default true if skipped
        "active_ok": active_result.get("ok", False),
    }

    results["summary"] = summary

    # Aggregate issues
    issues = []

    # Parse raw bars issues
    bars_data = load_json_results(out_dir / "raw_bars_health.json")
    low_coverage_bars = [
        f"{r['symbol']}.{r['day']}: {r['coverage']:.1%} coverage"
        for r in bars_data
        if r.get("coverage", 1.0) < 0.7
    ]
    issues.extend(low_coverage_bars)

    # Parse feature issues
    features_data = load_json_results(out_dir / "feature_columns_health.json")
    air_features = [
        f"{r['symbol']}.{r['feature']}: air column"
        for r in features_data
        if "error" not in r and (
            r.get("non_null_ratio", 1.0) < 0.1 or
            r.get("all_zero", False) or
            r.get("all_constant", False)
        )
    ]
    issues.extend(air_features)

    # Parse active issues
    active_data = load_json_results(out_dir / "active_coverage.json")
    underfilled_active = [
        f"{r['symbol']}: active underfilled (features={r.get('features_cov', 0):.1%}, active={r.get('active_cov', 0):.1%})"
        for r in active_data
        if r.get("active_underfilled", False)
    ]
    issues.extend(underfilled_active)

    results["issues"] = issues

    # Print summary
    status = "✅" if all(summary.values()) else "❌"
    print(f"{status} Overall Status: {'HEALTHY' if all(summary.values()) else 'ISSUES DETECTED'}")
    print()
    print(f"  Raw bars:  {'✅' if summary['raw_bars_ok'] else '❌'}")
    print(f"  Features:  {'✅' if summary['features_ok'] else '❌'}")
    print(f"  Labels:    {'✅' if summary['labels_ok'] else '⚠️  (skipped)' if results['checks']['labels'].get('skipped') else '❌'}")
    print(f"  Active:    {'✅' if summary['active_ok'] else '❌'}")

    if issues:
        print(f"\n⚠️  {len(issues)} Issues detected:")
        for issue in issues[:20]:  # Show first 20
            print(f"  - {issue}")
        if len(issues) > 20:
            print(f"  ... and {len(issues) - 20} more")

    # Save summary
    summary_path = out_dir / "summary.json"
    with open(summary_path, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\n✅ Summary saved to: {summary_path}")
    print(f"✅ Detailed results in: {out_dir}")

    # Exit code
    if not all(summary.values()):
        print("\n❌ ML Pipeline health check FAILED")
        return 1

    print("\n✅ ML Pipeline health check PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
