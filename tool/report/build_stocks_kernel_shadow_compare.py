#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_shadow_compare(*, left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    total = max(len(left), len(right))
    rows = []
    mismatches = 0
    for idx in range(total):
        l = left[idx] if idx < len(left) else None
        r = right[idx] if idx < len(right) else None
        same = l == r
        if not same:
            mismatches += 1
        rows.append({"index": idx, "left": l, "right": r, "matched": same})
    return {
        "left_count": len(left),
        "right_count": len(right),
        "mismatch_count": mismatches,
        "match_ratio": ((total - mismatches) / total) if total else 1.0,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two stocks-kernel shadow/replay outputs")
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    args = parser.parse_args()
    left = json.loads(Path(args.left).read_text(encoding="utf-8"))
    right = json.loads(Path(args.right).read_text(encoding="utf-8"))
    print(json.dumps(build_shadow_compare(left=left, right=right), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
