from __future__ import annotations

from typing import Any


def _field_diffs(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        if expected == actual:
            return []
        return [{"field": "__root__", "expected": expected, "actual": actual}]
    out = []
    keys = sorted(set(expected.keys()) | set(actual.keys()))
    for key in keys:
        if expected.get(key) != actual.get(key):
            out.append({"field": key, "expected": expected.get(key), "actual": actual.get(key)})
    return out


def build_stocks_kernel_parity_report(*, expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, Any]:
    total = max(len(expected), len(actual))
    comparisons = []
    matched = 0
    for idx in range(total):
        left = expected[idx] if idx < len(expected) else None
        right = actual[idx] if idx < len(actual) else None
        same = left == right
        if same:
            matched += 1
        comparisons.append({"index": idx, "expected": left, "actual": right, "matched": same, "field_diffs": _field_diffs(left, right)})
    return {
        "expected_count": len(expected),
        "actual_count": len(actual),
        "matched_count": matched,
        "match_ratio": (matched / total) if total else 1.0,
        "comparisons": comparisons,
    }
