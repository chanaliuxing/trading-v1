#!/usr/bin/env python3
"""
Test harness for DSL → SQL compilation using LLM‑generated cases.

Usage:
  venv/bin/python tools/test_feature_dsl_from_llm.py cases.json

Where cases.json is a JSON array of:
[
  {"name": "case_id", "dsl": { ... }},
  ...
]

The script will attempt to compile each DSL spec via libs.features.feature_dsl_sql.dsl_to_sql
and write a sibling cases.out.json with the result for each case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_sys_path() -> None:
    root = _project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _run_case(name: str, dsl_spec: Dict[str, Any]) -> Dict[str, Any]:
    from libs.features.feature_dsl_sql import dsl_to_sql, FeatureDSLCompileError

    result: Dict[str, Any] = {"name": name}
    try:
        sql = dsl_to_sql(dsl_spec)
        result["ok"] = True
        result["sql"] = sql
    except FeatureDSLCompileError as exc:
        result["ok"] = False
        result["error"] = str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        result["ok"] = False
        result["error"] = f"unexpected_error:{exc}"
    return result


def main(path: str) -> int:
    _ensure_sys_path()
    in_path = Path(path)
    if not in_path.exists():
        print(f"Input file not found: {in_path}", file=sys.stderr)
        return 1
    try:
        raw = json.loads(in_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to parse JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(raw, list):
        print("cases.json must be a JSON array", file=sys.stderr)
        return 1

    results: List[Dict[str, Any]] = []
    for idx, case in enumerate(raw):
        if not isinstance(case, dict):
            continue
        name = str(case.get("name") or f"case_{idx}")
        dsl_spec = case.get("dsl")
        if not isinstance(dsl_spec, dict):
            results.append({"name": name, "ok": False, "error": "dsl_spec_not_dict"})
            continue
        print(f"\n=== CASE: {name} ===")
        print(json.dumps(dsl_spec, indent=2, ensure_ascii=False))
        res = _run_case(name, dsl_spec)
        if res.get("ok"):
            print("SQL:", res.get("sql"))
        else:
            print("ERROR:", res.get("error"))
        results.append(res)

    out_path = in_path.with_suffix(".out.json")
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote results to {out_path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: venv/bin/python tools/test_feature_dsl_from_llm.py cases.json", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(main(sys.argv[1]))

