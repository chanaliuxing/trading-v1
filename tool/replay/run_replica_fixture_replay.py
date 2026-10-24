#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.strategy_runtime_v2.parity_recorder import compare_event_streams


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_fixture_report(
    *,
    catalog_path: Path,
    fixture_name: str,
    actual_trace_path: Path,
    qty_tolerance: float,
) -> Dict[str, Any]:
    catalog = _load_json(catalog_path)
    fixtures = dict(catalog.get("fixtures") or {})
    if fixture_name not in fixtures:
        raise KeyError(f"fixture not found: {fixture_name}")
    expected_rows = list((fixtures[fixture_name] or {}).get("events") or [])
    actual_trace = _load_json(actual_trace_path)
    actual_rows = list(actual_trace.get("events") or [])
    comparison = compare_event_streams(expected_rows, actual_rows, qty_tolerance=qty_tolerance)
    return {
        "catalog_path": str(catalog_path),
        "fixture_name": fixture_name,
        "fixture_category": (fixtures[fixture_name] or {}).get("category"),
        "actual_trace_path": str(actual_trace_path),
        "comparison": comparison,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare one replica fixture from catalog against an actual trace JSON")
    parser.add_argument(
        "--catalog",
        default=str(ROOT / "docs" / "baselines" / "index_super_win_replica_fixture_catalog.json"),
    )
    parser.add_argument("--fixture-name", required=True)
    parser.add_argument("--actual-trace", required=True)
    parser.add_argument("--qty-tolerance", type=float, default=1.0)
    parser.add_argument("--out")
    args = parser.parse_args()

    report = build_fixture_report(
        catalog_path=Path(args.catalog),
        fixture_name=str(args.fixture_name),
        actual_trace_path=Path(args.actual_trace),
        qty_tolerance=float(args.qty_tolerance),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if bool(report.get("comparison", {}).get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
