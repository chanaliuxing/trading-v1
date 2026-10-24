#!/usr/bin/env python3
"""
ML Drill Launcher

Kick off one ML review+execute cycle by simulating a completed ML job event.

Usage:
  python tools/run_ml_drill.py --job-id drill-001 --job-type oof_complete --ok false

Notes:
  - Expects runs/ai_trader/YYYY-MM-DD/oof_eval/*.json present for target symbols.
  - The consumer code will load compare.json/gating_policies/strategy configs if present.
  - This script is non-destructive; it triggers the same path as a real completion event.
"""
from __future__ import annotations
import argparse
import asyncio


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", default="drill-001")
    parser.add_argument("--job-type", default="oof_complete")
    parser.add_argument("--ok", default="false", help="true/false")
    parser.add_argument("--duration-sec", type=int, default=0)
    args = parser.parse_args()

    evt = {
        "job_id": args.job_id,
        "job_type": args.job_type,
        "ok": str(args.ok).lower() == "true",
        "duration_sec": int(args.duration_sec or 0),
        # metrics/artifacts optional; the handler loads context from runs/ files
    }

    from scripts.ai_trader.control_event_consumer import _handle_ml_completed
    await _handle_ml_completed(evt)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

