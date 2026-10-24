#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Check QQQ observability chain health from local diag jsonl.")
    parser.add_argument("--diag-path", required=True, help="Path to qqq_intraday_diag_*.jsonl")
    parser.add_argument("--strategy-id", default="qqq_intraday_001")
    parser.add_argument("--minutes", type=int, default=30)
    args = parser.parse_args()

    diag_path = Path(args.diag_path).expanduser()
    if not diag_path.exists():
        print(f"[ERROR] diag file not found: {diag_path}")
        return 2

    now = datetime.now(ET)
    since = now - timedelta(minutes=max(1, int(args.minutes)))
    counts: Counter[str] = Counter()
    loop_stage_counts: Counter[str] = Counter()
    newest_heartbeat_ts: datetime | None = None
    scanned = 0
    kept = 0

    with diag_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            scanned += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(rec.get("strategy_id") or args.strategy_id) != args.strategy_id:
                continue
            ts = _parse_ts(rec.get("ts_now_et"))
            if ts is None or ts.tzinfo is None:
                continue
            if ts < since:
                continue
            kept += 1
            event = str(rec.get("event") or "unknown")
            counts[event] += 1
            loop_stage = str(rec.get("loop_stage") or "")
            if loop_stage:
                loop_stage_counts[loop_stage] += 1
            if event == "qqq_loop_heartbeat":
                newest_heartbeat_ts = ts if newest_heartbeat_ts is None else max(newest_heartbeat_ts, ts)

    print(f"[INFO] strategy_id={args.strategy_id} scanned={scanned} kept={kept} since={since.isoformat()}")
    print("[INFO] event_counts:")
    for event, cnt in counts.most_common():
        print(f"  - {event}: {cnt}")
    if loop_stage_counts:
        print("[INFO] loop_stage_counts:")
        for stage, cnt in loop_stage_counts.most_common(10):
            print(f"  - {stage}: {cnt}")

    if newest_heartbeat_ts is None:
        print("[FAIL] no qqq_loop_heartbeat in window")
        return 1
    gap_sec = (now - newest_heartbeat_ts).total_seconds()
    print(f"[INFO] newest_heartbeat={newest_heartbeat_ts.isoformat()} gap_sec={gap_sec:.1f}")
    if gap_sec > 120:
        print("[FAIL] heartbeat gap too large (>120s)")
        return 1

    print("[PASS] observability chain baseline checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
