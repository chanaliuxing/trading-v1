#!/usr/bin/env python3
from __future__ import annotations

"""Quick harness to exercise strategy_experiment job via ml_scheduler.

Usage (dry-run style):
  python scripts/test_strategy_experiment_flow.py --symbols TSLA,AAPL \
      --start "2025-07-01 09:30:00" --end "2025-07-15 16:00:00"
"""

import argparse
import json
import sys
from pathlib import Path

from scripts.ai_trader.ml_scheduler import enqueue_job


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Test strategy_experiment job wiring.")
    ap.add_argument("--symbols", required=True, help="Comma-separated symbols")
    ap.add_argument("--start", required=True, help="Start timestamp (ISO)")
    ap.add_argument("--end", required=True, help="End timestamp (ISO)")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    job = {
        "job_type": "strategy_experiment",
        "priority": 60,
        "params": {
            "symbols": args.symbols,
            "start": args.start,
            "end": args.end,
        },
    }
    job_id = enqueue_job(job)
    print(json.dumps({"enqueued_job_id": job_id, "job": job}, indent=2))


if __name__ == "__main__":
    main()

