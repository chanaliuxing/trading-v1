#!/usr/bin/env python3
"""
Test DSL end-to-end: compilation → dry-run → validation.

Simulates AI Brain proposing DSL features:
1. Compile DSL to SQL
2. Run dry-run query against ClickHouse
3. Check if result is all-null/all-constant/error

Usage:
    venv/bin/python tools/test_dsl_end_to_end.py \
        --cases-file runs/ai_trader/2025-11-24/llm_dsl_cases.json \
        --out runs/tests/dsl_end_to_end.json

Input JSON format:
    [
      {
        "name": "ret_5_atr14_ratio",
        "dsl": {"op": "ratio", "num": "f.ret_5", "den": "f.atr_14"},
        "sources": ["f"]
      },
      ...
    ]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from libs.features.feature_dsl_sql import dsl_to_sql, FeatureDSLCompileError
from scripts.ai_trader.brain.tools_sql import dry_run_feature_expr


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Test DSL end-to-end"
    )
    ap.add_argument(
        "--cases-file",
        required=True,
        help="JSON file with DSL test cases"
    )
    ap.add_argument(
        "--sample-symbol",
        default="TSLA",
        help="Symbol to use for dry-run (default: TSLA)"
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Row limit for dry-run (default: 10)"
    )
    ap.add_argument(
        "--out",
        help="Output JSON file path"
    )
    return ap.parse_args()


def test_dsl_case(
    case: Dict[str, Any],
    sample_symbol: str,
    limit: int
) -> Dict[str, Any]:
    """
    Test a single DSL case end-to-end.

    Args:
        case: {"name": "...", "dsl": {...}, "sources": [...]}
        sample_symbol: Symbol for dry-run
        limit: Row limit for dry-run

    Returns:
        {
            "name": "ret_5_atr14_ratio",
            "compile_ok": true,
            "expr": "(f.ret_5) / nullIf(f.atr_14, 1e-06)",
            "dry_run_ok": true,
            "all_null": false,
            "error": null
        }
    """
    name = case.get("name", "unnamed")
    dsl_spec = case.get("dsl")
    sources = case.get("sources", [])

    result: Dict[str, Any] = {
        "name": name,
        "compile_ok": False,
        "dry_run_ok": False,
    }

    # Step 1: Compile DSL to SQL
    try:
        expr = dsl_to_sql(dsl_spec)
        result["compile_ok"] = True
        result["expr"] = expr
    except FeatureDSLCompileError as e:
        result["error"] = f"Compile error: {e}"
        return result
    except Exception as e:
        result["error"] = f"Unexpected compile error: {e}"
        return result

    # Step 2: Dry-run SQL expression
    try:
        dry_run_result = dry_run_feature_expr(
            expr=expr,
            sources=sources,
            sample_symbol=sample_symbol,
            limit=limit,
            save=False  # Don't save individual dry-run results
        )
    except Exception as e:
        result["error"] = f"Dry-run exception: {e}"
        return result

    # Step 3: Parse dry-run result
    if not dry_run_result.get("ok"):
        result["error"] = dry_run_result.get("error_message", "Unknown dry-run error")
        result["error_type"] = dry_run_result.get("error_type", "UnknownError")
        return result

    result["dry_run_ok"] = True

    # Check if all values are NULL
    rows_preview = dry_run_result.get("rows_preview", [])
    if rows_preview:
        # Check if all 'value' fields are NULL
        all_null = all(r.get("value") is None for r in rows_preview)
        result["all_null"] = all_null

        # Check if all non-null values are the same (constant)
        non_null_values = [r.get("value") for r in rows_preview if r.get("value") is not None]
        if len(non_null_values) > 1:
            all_constant = len(set(non_null_values)) == 1
            result["all_constant"] = all_constant
        else:
            result["all_constant"] = False
    else:
        result["all_null"] = True
        result["all_constant"] = False

    result["error"] = None
    return result


def main():
    args = parse_args()

    cases_path = Path(args.cases_file)
    if not cases_path.exists():
        print(f"❌ Cases file not found: {cases_path}", file=sys.stderr)
        return 1

    # Load test cases
    try:
        with open(cases_path, 'r') as f:
            cases = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(cases, list):
        print(f"❌ Cases file must contain a JSON array", file=sys.stderr)
        return 1

    print(f"Testing DSL end-to-end:")
    print(f"  Cases file: {cases_path}")
    print(f"  Total cases: {len(cases)}")
    print(f"  Sample symbol: {args.sample_symbol}")
    print()

    results: List[Dict[str, Any]] = []

    for i, case in enumerate(cases, 1):
        name = case.get("name", f"case_{i}")
        print(f"[{i}/{len(cases)}] Testing: {name}...", end=" ")

        result = test_dsl_case(case, args.sample_symbol, args.limit)
        results.append(result)

        # Print status
        if not result["compile_ok"]:
            print(f"❌ COMPILE FAILED: {result.get('error', 'Unknown')}")
        elif not result["dry_run_ok"]:
            print(f"❌ DRY-RUN FAILED: {result.get('error', 'Unknown')}")
        elif result.get("all_null"):
            print(f"⚠️  ALL NULL")
        elif result.get("all_constant"):
            print(f"⚠️  ALL CONSTANT")
        else:
            print(f"✅ OK")

    # Summary statistics
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)

    total = len(results)
    compile_ok = sum(1 for r in results if r["compile_ok"])
    dry_run_ok = sum(1 for r in results if r["dry_run_ok"])
    all_null = sum(1 for r in results if r.get("all_null", False))
    all_constant = sum(1 for r in results if r.get("all_constant", False))
    healthy = sum(
        1 for r in results
        if r["compile_ok"]
        and r["dry_run_ok"]
        and not r.get("all_null", False)
        and not r.get("all_constant", False)
    )

    print(f"Total cases: {total}")
    print(f"Compile OK: {compile_ok}/{total} ({compile_ok/total*100:.1f}%)")
    print(f"Dry-run OK: {dry_run_ok}/{compile_ok} ({dry_run_ok/compile_ok*100:.1f}% of compiled)")
    print(f"Healthy (not null/constant): {healthy}/{total} ({healthy/total*100:.1f}%)")
    print(f"All-null results: {all_null}")
    print(f"All-constant results: {all_constant}")

    # List failures
    compile_failures = [r for r in results if not r["compile_ok"]]
    if compile_failures:
        print(f"\n⚠️  Compile failures:")
        for r in compile_failures[:10]:  # Show first 10
            print(f"  - {r['name']}: {r.get('error', 'Unknown')}")

    dry_run_failures = [r for r in results if r["compile_ok"] and not r["dry_run_ok"]]
    if dry_run_failures:
        print(f"\n⚠️  Dry-run failures:")
        for r in dry_run_failures[:10]:  # Show first 10
            print(f"  - {r['name']}: {r.get('error', 'Unknown')}")

    # Output JSON
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\n✅ Results saved to: {out_path}")
    else:
        print("\n" + json.dumps(results, indent=2))

    # Exit code based on quality
    if compile_ok < total:
        print(f"\n❌ {total - compile_ok} DSL cases failed to compile!")
        return 1

    if healthy < total * 0.8:
        print(f"\n⚠️  Only {healthy}/{total} cases are healthy (<80%)!")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
