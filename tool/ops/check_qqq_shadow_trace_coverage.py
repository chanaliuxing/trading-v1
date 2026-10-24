#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ET = ZoneInfo("America/New_York")
TRACE_ROOT = REPO_ROOT / "reports" / "qqq_signal_snapshots"
LIVE_EXPECTED = "qqq_intraday_001"
LIVE_ALIASES = (
    "qqq_intraday_001",
    "qqq_intraday_option_v1_001",
)
EXPECTED_PAPER_TRACE_STRATEGIES = (
    "qqq_kernel_legacy_on_001",
    "qqq_kernel_legacy_off_001",
)


def _today() -> str:
    return datetime.now(ET).strftime("%Y%m%d")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def build_trace_coverage_report(*, day: str, trace_root: Path = TRACE_ROOT) -> dict[str, Any]:
    file_rows: list[dict[str, Any]] = []
    strategy_counts: dict[str, dict[str, int]] = {}
    for path in sorted(trace_root.glob(f"qqq_signal_snapshots_{day}*.jsonl")):
        rows = _read_jsonl(path)
        file_strategy_ids = sorted({str(row.get("strategy_id") or "").strip() for row in rows if str(row.get("strategy_id") or "").strip()})
        file_rows.append(
            {
                "path": str(path),
                "row_count": len(rows),
                "strategy_ids": file_strategy_ids,
            }
        )
        for row in rows:
            strategy_id = str(row.get("strategy_id") or "").strip()
            if not strategy_id:
                continue
            bucket = strategy_counts.setdefault(strategy_id, {"rows": 0, "evaluation": 0, "entry": 0})
            bucket["rows"] += 1
            kind = str(row.get("snapshot_kind") or "entry").strip().lower()
            if kind == "evaluation":
                bucket["evaluation"] += 1
            elif kind == "entry":
                bucket["entry"] += 1

    matched_live_strategy_ids = [strategy_id for strategy_id in LIVE_ALIASES if strategy_id in strategy_counts]
    missing = []
    if not matched_live_strategy_ids:
        missing.append(LIVE_EXPECTED)
    missing.extend(strategy_id for strategy_id in EXPECTED_PAPER_TRACE_STRATEGIES if strategy_id not in strategy_counts)
    recommendation = "PASS" if not missing else "HOLD"
    return {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "day": day,
        "recommendation": recommendation,
        "expected_strategy_ids": [LIVE_EXPECTED, *EXPECTED_PAPER_TRACE_STRATEGIES],
        "missing_strategy_ids": missing,
        "file_count": len(file_rows),
        "files": file_rows,
        "strategy_counts": strategy_counts,
        "live_writer_present": bool(matched_live_strategy_ids),
        "matched_live_strategy_ids": matched_live_strategy_ids,
        "legacy_writer_present": "qqq_intraday_001" in strategy_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check same-day QQQ paper/live trace coverage")
    parser.add_argument("--day", default=_today())
    parser.add_argument("--trace-root", default=str(TRACE_ROOT))
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    payload = build_trace_coverage_report(day=str(args.day), trace_root=Path(args.trace_root))
    output = Path(args.output) if str(args.output or "").strip() else Path(args.trace_root).parent / "qqq_kernel_paper_trace" / f"{args.day}_trace_coverage.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"day": payload["day"], "recommendation": payload["recommendation"], "missing_strategy_ids": payload["missing_strategy_ids"]}, ensure_ascii=False, sort_keys=True))
    return 0 if not payload["missing_strategy_ids"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
