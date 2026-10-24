#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_reason_diff_report(*, parity_report: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in parity_report.get("comparisons", []):
        if item.get("matched", False):
            continue
        rows.append(
            {
                "index": item.get("index"),
                "field_diffs": item.get("field_diffs", []),
                "expected": item.get("expected"),
                "actual": item.get("actual"),
            }
        )
    return {
        "mismatch_count": len(rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build reason diff report from stocks-kernel parity report")
    parser.add_argument("--parity", required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.parity).read_text(encoding="utf-8"))
    print(json.dumps(build_reason_diff_report(parity_report=report), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
