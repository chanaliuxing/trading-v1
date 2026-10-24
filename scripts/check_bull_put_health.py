#!/usr/bin/env python3
"""
Quick health check for Bull Put Spread strategy audit logs.

Reads runs/ai_trader/<date>/bull_put_spread.jsonl, computes
 - entry_filled / enter_submitted ratio
 - counts per state

Exits non-zero if the audit file is missing or ratio falls below threshold.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Tuple


def load_events(run_dir: Path, target_date: date) -> list[dict]:
    day_dir = run_dir / target_date.isoformat()
    audit_path = day_dir / "bull_put_spread.jsonl"
    if not audit_path.exists():
        raise FileNotFoundError(f"Audit file not found: {audit_path}")
    events = []
    with audit_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def compute_metrics(events: list[dict]) -> Tuple[float, Counter, int, int]:
    submitted = sum(1 for e in events if e.get("event") == "enter_submitted")
    filled = sum(1 for e in events if e.get("event") == "entry_filled")
    states = Counter(e.get("state", "UNKNOWN") for e in events if e.get("state"))
    return (filled / submitted) if submitted else 0.0, states, submitted, filled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD; default today", default=date.today().isoformat())
    parser.add_argument("--ratio-threshold", type=float, default=0.5, help="Minimum acceptable entry fill ratio")
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    run_dir = Path(os.getenv("RUNS_DIR", "runs/ai_trader"))

    try:
        events = load_events(run_dir, target_date)
    except FileNotFoundError as exc:
        print(f"[WARN] {exc}")
        return 2

    ratio, states, submitted, filled = compute_metrics(events)

    print(f"Date: {target_date}")
    print(f"enter_submitted={submitted}, entry_filled={filled}, ratio={ratio:.2f}")
    print("state_counts:")
    for state, count in states.items():
        print(f"  {state}: {count}")

    if submitted == 0:
        print("No submissions recorded; skipping ratio check.")
        return 0

    if ratio < args.ratio_threshold:
        print(f"[ALERT] entry_filled/enter_submitted ratio {ratio:.2f} below threshold {args.ratio_threshold:.2f}")
        return 1

    print("Bull Put Spread audit metrics look healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
