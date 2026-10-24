#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _as_pair_key(item: dict[str, Any]) -> str:
    legacy_reason = str(((item.get("legacy") or {}).get("reason")) or "").strip() or "UNKNOWN"
    kernel_reason = str(((item.get("kernel") or {}).get("reason")) or "").strip() or "UNKNOWN"
    return f"{legacy_reason} -> {kernel_reason}"


def _hour_bucket(ts_iso: str) -> str:
    text = str(ts_iso or "").strip()
    if "T" not in text:
        return "unknown"
    try:
        hour = text.split("T", 1)[1][:2]
        return f"{hour}:00"
    except Exception:
        return "unknown"


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    mismatches = list(payload.get("mismatches") or [])
    pair_counter: Counter[str] = Counter()
    hour_counter_by_pair: dict[str, Counter[str]] = defaultdict(Counter)
    sample_by_pair: dict[str, dict[str, Any]] = {}

    for item in mismatches:
        pair = _as_pair_key(item)
        pair_counter[pair] += 1
        hour_counter_by_pair[pair][_hour_bucket(str(item.get("ts_iso") or ""))] += 1
        sample_by_pair.setdefault(pair, item)

    hard_mismatch_count = 0
    for item in mismatches:
        legacy = dict(item.get("legacy") or {})
        kernel = dict(item.get("kernel") or {})
        legacy_allowed = bool(legacy.get("allowed"))
        kernel_allowed = bool(kernel.get("allowed"))
        legacy_side = str(legacy.get("side") or "").strip().upper()
        kernel_side = str(kernel.get("side") or "").strip().upper()
        legacy_exec = str(legacy.get("instrument") or "").strip().upper()
        kernel_exec = str(kernel.get("exec_symbol") or "").strip().upper()
        if kernel_allowed == legacy_allowed and kernel_side == legacy_side and (not kernel_allowed or kernel_exec == legacy_exec):
            continue
        hard_mismatch_count += 1

    rows = int(payload.get("rows") or 0)
    mismatch_count = int(payload.get("mismatch_count") or 0)
    pair_rows: list[dict[str, Any]] = []
    for pair, count in pair_counter.most_common():
        sample = sample_by_pair.get(pair) or {}
        pair_rows.append(
            {
                "pair": pair,
                "count": int(count),
                "ratio_vs_rows": round(float(count) / max(1, rows), 6),
                "hours": dict(hour_counter_by_pair[pair]),
                "sample": {
                    "ts_iso": sample.get("ts_iso"),
                    "legacy": sample.get("legacy"),
                    "kernel": sample.get("kernel"),
                },
            }
        )

    return {
        "ok": bool(payload.get("ok")),
        "day": payload.get("day"),
        "rows": rows,
        "mismatch_count": mismatch_count,
        "mismatch_ratio": round(float(mismatch_count) / max(1, rows), 6),
        "hard_mismatch_count": hard_mismatch_count,
        "hard_mismatch_ratio": round(float(hard_mismatch_count) / max(1, rows), 6),
        "parity": dict(payload.get("parity") or {}),
        "top_reason_pairs": pair_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize qqq kernel identity mismatch report")
    parser.add_argument("--report", required=True, help="path to qqq_kernel_identity_check_*.json")
    parser.add_argument("--output", default="", help="optional output summary json path")
    args = parser.parse_args()

    report_path = Path(args.report).expanduser()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    out = summarize(payload)
    rendered = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True)
    if str(args.output).strip():
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
