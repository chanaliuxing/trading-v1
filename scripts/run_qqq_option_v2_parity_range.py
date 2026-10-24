#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.options_kernel.harness import run_clickhouse_day_parity_scenario_sync


def _iter_days(start: str, end: str) -> List[str]:
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if end_day < start_day:
        raise ValueError("end date must be >= start date")
    out: List[str] = []
    cur = start_day
    while cur <= end_day:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    matched_days = [row["day"] for row in results if bool(row.get("matched"))]
    mismatched_days = [row["day"] for row in results if not bool(row.get("matched"))]
    total_old = sum(int(row.get("old_count") or 0) for row in results)
    total_new = sum(int(row.get("new_count") or 0) for row in results)
    return {
        "days_total": len(results),
        "matched_days": matched_days,
        "mismatched_days": mismatched_days,
        "matched_day_count": len(matched_days),
        "mismatched_day_count": len(mismatched_days),
        "day_parity_rate": (float(len(matched_days)) / float(len(results))) if results else 1.0,
        "total_old_count": total_old,
        "total_new_count": total_new,
        "count_parity_ok": total_old == total_new,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a date-range ClickHouse-backed parity check for legacy vs v2 QQQ option strategy.")
    ap.add_argument("--start", required=True, help="Start date in YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="End date in YYYY-MM-DD, inclusive")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--session-mode", default="extended", choices=["rth", "extended"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    results: List[Dict[str, Any]] = []
    for day in _iter_days(args.start, args.end):
        payload = run_clickhouse_day_parity_scenario_sync(
            {
                "day": day,
                "symbol": args.symbol,
                "session_mode": args.session_mode,
                "snapshot_root": f"runtime/state/decision_snapshots_day_parity/{day}_{os.getpid()}",
            }
        )
        results.append(
            {
                "day": day,
                "matched": bool(payload.get("matched")),
                "old_count": int(payload.get("old_count") or 0),
                "new_count": int(payload.get("new_count") or 0),
                "diff": payload.get("diff") or {},
            }
        )

    output = {
        "generated_at": datetime.now().isoformat(),
        "symbol": args.symbol,
        "session_mode": args.session_mode,
        "start": args.start,
        "end": args.end,
        "summary": _summarize(results),
        "days": results,
    }
    text = json.dumps(output, ensure_ascii=False, indent=2, default=str)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
