#!/usr/bin/env python3
"""Update hedging Prometheus metrics from position tracker.

This script should run periodically (e.g., every 5 minutes) to update:
- Position counts by underlying
- Expiring positions count
- Portfolio Greeks (delta, gamma, vega, theta)
- Unrealized P&L
- Daily execution stats

Usage:
    python scripts/ai_trader/risk/update_hedging_metrics.py --runs-dir runs/ai_trader
"""

from __future__ import annotations
import asyncio
import argparse
from pathlib import Path
from datetime import date

from scripts.ai_trader.risk.options_positions import get_position_tracker
from scripts.ai_trader.risk.hedging_events import HedgingEventsLogger


async def update_position_metrics(runs_dir: str):
    """Update position-related metrics.

    Args:
        runs_dir: Base runs directory
    """
    tracker = get_position_tracker(runs_dir)

    # Update market data first (if IBKR backend available)
    try:
        await tracker.update_market_data()
    except Exception:
        # Market data update failed - proceed with stale data
        pass

    # Update metrics
    tracker.update_metrics()


def update_daily_stats(runs_dir: str):
    """Update daily execution stats from events log.

    Args:
        runs_dir: Base runs directory
    """
    logger = HedgingEventsLogger(runs_dir)
    stats = logger.get_daily_stats()

    # Count contracts and notional from today's events
    contracts_today = 0
    notional_today = 0.0

    # Read today's execution events
    events_file = Path(runs_dir) / date.today().isoformat() / "hedging_events.jsonl"
    if events_file.exists():
        import json

        with events_file.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue

                try:
                    event = json.loads(line)
                    if event.get("event_type") == "execution" and event.get("success"):
                        details = event.get("details", {})
                        if not details.get("dry_run"):
                            # Real execution
                            contracts_today += details.get("qty", 0)
                            notional_today += details.get("notional", 0.0)
                except Exception:
                    continue

    # Update metrics
    tracker = get_position_tracker(runs_dir)
    tracker.update_daily_stats(contracts_today, notional_today)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Update hedging metrics")
    parser.add_argument(
        "--runs-dir",
        default="runs/ai_trader",
        help="Runs directory (default: runs/ai_trader)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously with 5-minute interval",
    )
    args = parser.parse_args()

    if args.loop:
        # Continuous mode
        print(f"Starting metrics updater (loop mode, interval=300s)...")
        while True:
            try:
                await update_position_metrics(args.runs_dir)
                update_daily_stats(args.runs_dir)
                print(f"✓ Updated metrics at {date.today().isoformat()}")
            except Exception as e:
                print(f"⚠ Error updating metrics: {e}")

            # Sleep 5 minutes
            await asyncio.sleep(300)
    else:
        # Single run
        await update_position_metrics(args.runs_dir)
        update_daily_stats(args.runs_dir)
        print("✓ Metrics updated")


if __name__ == "__main__":
    asyncio.run(main())
