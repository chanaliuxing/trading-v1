#!/usr/bin/env python3
from __future__ import annotations
"""
Validate DSL-based feature specs end-to-end:

1. Load DSL specs from config/features_catalog.json (candidate section)
2. Compile DSL → ClickHouse SQL via libs.features.feature_dsl_sql.dsl_to_sql
3. Dry-run each expression against ClickHouse using dry_run_feature_expr
4. Report compile / ClickHouse errors and all-null / constant features

Usage:
    source venv/bin/activate
    venv/bin/python tools/validate_dsl_features.py \
        --catalog config/features_catalog.json \
        --sample-symbol TSLA \
        --limit 10 \
        --out runs/tests/dsl_catalog_validation.json
"""

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
        description="Validate DSL-based feature specs from features_catalog.json"
    )
    ap.add_argument(
        "--catalog",
        default="config/features_catalog.json",
        help="Path to features catalog JSON (default: config/features_catalog.json)",
    )
    ap.add_argument(
        "--sample-symbol",
        default="TSLA",
        help="Symbol to use for ClickHouse dry-run (default: TSLA)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Row limit for dry-run (default: 10)",
    )
    ap.add_argument(
        "--out",
        help="Optional output JSON file for detailed results",
    )
    return ap.parse_args()


def _load_catalog(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"Catalog not found: {path}")
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception as exc:
        raise SystemExit(f"Failed to parse catalog {path}: {exc}")
    candidates = data.get("candidate") or []
    specs = [c for c in candidates if isinstance(c, dict) and c.get("name")]
    return specs


def _auto_sources_from_expr(expr: str) -> List[str]:
    expr_lower = expr.lower()
    srcs: List[str] = []
    for alias in ("m5", "d"):
        if f"{alias}." in expr_lower:
            srcs.append(alias)
    # f. is implicit base; we do not treat it as a separate source alias here.
    return sorted(set(srcs))


def validate_spec(
    spec: Dict[str, Any],
    sample_symbol: str,
    limit: int,
) -> Dict[str, Any]:
    """
    Validate a single DSL-based feature spec:
      - compile DSL → SQL
      - dry-run SQL against ClickHouse

    Returns result dict with keys:
      name, compile_ok, expr, dry_run_ok, error_type, error_message,
      all_null, all_constant
    """
    name = str(spec.get("name") or "unnamed")
    dsl_spec = spec.get("dsl")

    result: Dict[str, Any] = {
        "name": name,
        "compile_ok": False,
        "dry_run_ok": False,
    }

    if not isinstance(dsl_spec, dict):
        result["error_type"] = "NoDSL"
        result["error_message"] = "Spec has no 'dsl' field or it is not a dict"
        return result

    # Step 1: compile DSL → SQL
    try:
        expr = dsl_to_sql(dsl_spec)
        result["compile_ok"] = True
        result["expr"] = expr
    except FeatureDSLCompileError as exc:
        result["error_type"] = "DSLCompileError"
        result["error_message"] = str(exc)
        return result
    except Exception as exc:
        result["error_type"] = "UnexpectedCompileError"
        result["error_message"] = str(exc)
        return result

    # Step 2: dry-run SQL expression
    try:
        required_sources = spec.get("required_sources") or []
        if isinstance(required_sources, list):
            sources = [str(s) for s in required_sources]
        else:
            sources = []
        # Auto-detect sources from expression as a fallback
        if not sources:
            sources = _auto_sources_from_expr(expr)
        dry = dry_run_feature_expr(
            expr=expr,
            sources=sources,
            sample_symbol=sample_symbol,
            limit=limit,
            save=False,
        )
    except Exception as exc:
        result["error_type"] = "DryRunException"
        result["error_message"] = str(exc)
        return result

    if not isinstance(dry, dict) or not dry.get("ok", False):
        result["error_type"] = dry.get("error_type", "DryRunFailed") if isinstance(dry, dict) else "DryRunFailed"
        result["error_message"] = dry.get("error_message", "dry_run_feature_expr failed") if isinstance(dry, dict) else "dry_run_feature_expr failed"
        return result

    result["dry_run_ok"] = True

    # Step 3: judge all-null / all-constant
    rows = dry.get("rows_preview") or []
    vals = []
    for r in rows:
        if isinstance(r, dict) and "value" in r:
            vals.append(r.get("value"))
    non_null = [v for v in vals if v is not None]
    if not non_null:
        result["all_null"] = True
        result["all_constant"] = False
    else:
        distinct = {v for v in non_null}
        result["all_null"] = False
        result["all_constant"] = len(distinct) == 1 and len(non_null) >= 5

    return result


def main() -> int:
    args = parse_args()
    catalog_path = Path(args.catalog)
    specs = _load_catalog(catalog_path)

    # Only consider specs that have a DSL payload; hand-written expr-only
    # specs are validated via test_feature_catalog_sql.py.
    dsl_specs = [s for s in specs if isinstance(s.get("dsl"), dict)]
    if not dsl_specs:
        print(f"No DSL-based specs found in {catalog_path}")
        return 0

    print(f"Validating DSL specs from {catalog_path}:")
    print(f"  Total candidate specs: {len(specs)}")
    print(f"  DSL-based specs: {len(dsl_specs)}")
    print(f"  Sample symbol: {args.sample_symbol}")
    print()

    results: List[Dict[str, Any]] = []
    total = len(dsl_specs)

    for idx, spec in enumerate(dsl_specs, 1):
        name = spec.get("name", f"spec_{idx}")
        print(f"[{idx}/{total}] {name} ... ", end="", flush=True)
        res = validate_spec(spec, args.sample_symbol, args.limit)
        results.append(res)

        if not res["compile_ok"]:
            print(f"❌ COMPILE: {res.get('error_type','?')} - {res.get('error_message','')}")
        elif not res["dry_run_ok"]:
            print(f"❌ DRY-RUN: {res.get('error_type','?')} - {res.get('error_message','')}")
        elif res.get("all_null"):
            print("⚠️  ALL NULL")
        elif res.get("all_constant"):
            print("⚠️  ALL CONSTANT")
        else:
            print("✅ OK")

    # Summary
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)

    compile_ok = sum(1 for r in results if r["compile_ok"])
    dry_ok = sum(1 for r in results if r["dry_run_ok"])
    all_null = sum(1 for r in results if r.get("all_null"))
    all_const = sum(1 for r in results if r.get("all_constant"))
    healthy = sum(
        1 for r in results
        if r["compile_ok"] and r["dry_run_ok"] and not r.get("all_null") and not r.get("all_constant")
    )

    print(f"Total DSL specs: {len(dsl_specs)}")
    print(f"Compile OK: {compile_ok}/{len(dsl_specs)}")
    print(f"Dry-run OK: {dry_ok}/{compile_ok if compile_ok else 1}")
    print(f"Healthy (not null/constant): {healthy}/{len(dsl_specs)}")
    print(f"All-null: {all_null}")
    print(f"All-constant: {all_const}")

    # Optional JSON output
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\n✅ Detailed results saved to: {out_path}")

    # Exit code：编译失败或严重问题时非 0，供 CI/夜间 job 使用
    if compile_ok < len(dsl_specs):
        print(f"\n❌ {len(dsl_specs) - compile_ok} DSL specs failed to compile.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

