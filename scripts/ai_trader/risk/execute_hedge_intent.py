#!/usr/bin/env python3
"""
Execute Hedge Intent - CLI wrapper for hedge intent execution.

This script is called by orchestrator to execute hedge intents from brain plans.
It bridges the old execute_hedge.py approach with the new HedgeIntentExecutor.

Usage:
    python scripts/ai_trader/risk/execute_hedge_intent.py [--runs-dir DIR] [--session SESSION] [--force]
"""

import sys
import asyncio
import argparse
import json
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from libs.logger import get_logger
from llm_trader.plan_types import HedgeIntent
from scripts.ai_trader.risk.hedge_intent_executor import HedgeIntentExecutor

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Execute hedge from brain plan")
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs/ai_trader"),
        help="Runs directory (default: runs/ai_trader)",
    )
    parser.add_argument(
        "--session",
        type=str,
        default="intraday",
        choices=["morning", "intraday", "afternoon", "afterhours"],
        help="Session to load plan from (default: intraday)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force execution even without hedge_intent in plan",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override config and run in dry-run mode",
    )
    args = parser.parse_args()

    # Load plan file
    today = date.today().isoformat()
    plan_file = args.runs_dir / today / f"brain_plan_{args.session}.json"

    if not plan_file.exists():
        logger.warning(
            "plan_file_not_found",
            file=str(plan_file),
            session=args.session,
        )
        print(json.dumps({
            "success": False,
            "error": f"Plan file not found: {plan_file}",
        }, indent=2))
        return 1

    # Load plan
    try:
        plan_data = json.loads(plan_file.read_text())
    except Exception as e:
        logger.error("plan_load_error", file=str(plan_file), error=str(e))
        print(json.dumps({
            "success": False,
            "error": f"Failed to load plan: {e}",
        }, indent=2))
        return 1

    # Extract hedge_intent
    hedge_intent_data = plan_data.get("hedge_intent")

    if not hedge_intent_data and not args.force:
        logger.info(
            "no_hedge_intent",
            session=args.session,
            file=str(plan_file),
        )
        print(json.dumps({
            "success": False,
            "skipped": True,
            "reason": "No hedge_intent in plan (use --force to execute anyway)",
        }, indent=2))
        return 0

    # Parse hedge intent
    hedge_intent = None
    if hedge_intent_data:
        try:
            hedge_intent = HedgeIntent(**hedge_intent_data)
            logger.info(
                "hedge_intent_loaded",
                symbols=hedge_intent.symbols,
                notional_pct=hedge_intent.notional_pct,
                urgency=hedge_intent.urgency,
                strategy=hedge_intent.strategy,
            )
        except Exception as e:
            logger.error("hedge_intent_parse_error", error=str(e), data=hedge_intent_data)
            print(json.dumps({
                "success": False,
                "error": f"Failed to parse hedge_intent: {e}",
            }, indent=2))
            return 1

    # Create executor
    executor = HedgeIntentExecutor(runs_dir=args.runs_dir)

    # Override dry-run if requested
    if args.dry_run:
        executor.hedge_executor.dry_run = True
        logger.info("dry_run_override", msg="Forcing dry-run mode")

    # Execute
    logger.info(
        "executing_hedge_intent",
        session=args.session,
        has_intent=hedge_intent is not None,
        force=args.force,
    )

    try:
        result = await executor.process_hedge_intent(
            hedge_intent=hedge_intent,
            force=args.force,
        )

        # Print result
        print(json.dumps(result, indent=2, default=str))

        if result.get("success") or result.get("skipped"):
            logger.info(
                "hedge_execution_complete",
                success=result.get("success"),
                action=result.get("action"),
                skipped=result.get("skipped"),
            )
            return 0
        else:
            logger.error(
                "hedge_execution_failed",
                error=result.get("error"),
                reason=result.get("reason"),
            )
            return 1

    except Exception as e:
        logger.error("hedge_execution_exception", error=str(e), exc_info=True)
        print(json.dumps({
            "success": False,
            "error": str(e),
        }, indent=2))
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
