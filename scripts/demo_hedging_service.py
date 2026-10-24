#!/usr/bin/env python3
"""
Demo: Run HedgingService in standalone mode with test data.

This script simulates the hedging service running continuously
and making decisions based on the test portfolio.
"""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.risk.hedging_service import HedgingService
from libs.logger import get_logger

logger = get_logger(__name__)


async def main():
    """Run hedging service demo."""
    logger.info("=" * 70)
    logger.info("🛡️  HedgingService DEMO - Standalone Mode")
    logger.info("=" * 70)
    logger.info("")
    logger.info("Test Portfolio:")
    logger.info("  - AAPL: $20,000 long")
    logger.info("  - TSLA: $15,000 long")
    logger.info("  - NVDA: $10,000 long")
    logger.info("  - Total Net Delta: $45,000 (90% of max)")
    logger.info("")
    logger.info("This demo will run 3 hedging cycles with 10-second intervals")
    logger.info("Press Ctrl+C to stop anytime")
    logger.info("=" * 70)
    logger.info("")

    # Create hedging service
    hs = HedgingService()

    # Override config for demo
    hs.enabled = True
    hs.dry_run = True
    hs.check_interval_sec = 10  # Faster for demo
    hs.cooldown_sec = 0  # No cooldown for demo

    logger.info("Starting HedgingService (DRY RUN mode)...")
    logger.info("")

    # Run 3 cycles manually
    for cycle in range(1, 4):
        logger.info(f"{'─' * 70}")
        logger.info(f"Cycle {cycle}/3")
        logger.info(f"{'─' * 70}")

        try:
            await hs._run_cycle()
        except Exception as e:
            logger.error(f"Cycle {cycle} error", error=str(e), exc_info=True)

        if cycle < 3:
            logger.info("")
            logger.info(f"Waiting 10 seconds before next cycle...")
            logger.info("")
            await asyncio.sleep(10)

    logger.info("")
    logger.info("=" * 70)
    logger.info("✅ Demo completed!")
    logger.info("=" * 70)
    logger.info("")
    logger.info("What happened:")
    logger.info("1. HedgingService read the test portfolio ($45k long)")
    logger.info("2. Calculated SPY trend score (downtrend → more hedge)")
    logger.info("3. Determined target hedge ratio: 100%")
    logger.info("4. Recommended buying $45k worth of SPY PUTs")
    logger.info("")
    logger.info("In real mode (dry_run=False):")
    logger.info("- Would call OptionsSelector to pick contracts")
    logger.info("- Would submit orders via ExecutionService")
    logger.info("- Would respect theta budget constraints")
    logger.info("")
    logger.info("Next steps:")
    logger.info("1. Review START_HEDGING_MVP.md")
    logger.info("2. Enable in ai_trader.yaml: hedge.enabled = true")
    logger.info("3. Start run_system.py normally")
    logger.info("")
    logger.info("Clean up test data:")
    logger.info("  python scripts/insert_test_signals.py --cleanup")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nDemo stopped by user")
    except Exception as e:
        logger.error("Demo failed", error=str(e), exc_info=True)
        sys.exit(1)
