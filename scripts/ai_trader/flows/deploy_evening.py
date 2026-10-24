#!/usr/bin/env python3
"""
Create and register a real Prefect Deployment for the evening learning flow.

Requirements:
  pip install prefect
  prefect server start &
  prefect agent start -q 'default' &

Usage:
  python scripts/ai_trader/flows/deploy_evening.py \
    --cron "0 20 * * *" \
    --timezone "America/New_York" \
    --name "AI Trader - Evening Learning" \
    --queue "default"

After applying, schedule and runs are visible in the Prefect UI.
"""

from __future__ import annotations

import argparse
from typing import Optional

from prefect.deployments import Deployment
from prefect.server.schemas.schedules import CronSchedule

from scripts.ai_trader.flows.prefect_evening import evening_learning_flow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cron", default="0 20 * * *", help="Cron string for schedule (NY 20:00 by default)")
    ap.add_argument("--timezone", default="America/New_York", help="Timezone for the schedule")
    ap.add_argument("--name", default="AI Trader - Evening Learning", help="Deployment name")
    ap.add_argument("--queue", default="default", help="Work queue name for agent")
    args = ap.parse_args()

    dep = Deployment.build_from_flow(
        flow=evening_learning_flow,
        name=args.name,
        schedule=CronSchedule(cron=args.cron, timezone=args.timezone),
        work_queue_name=args.queue,
        tags=["ai_trader", "evening"],
        parameters={},
    )
    dep.apply()
    print(f"✓ Deployment applied: {args.name} (queue={args.queue}, cron='{args.cron}', tz={args.timezone})")


if __name__ == "__main__":
    main()

