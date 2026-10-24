"""
Feature Service Main Entry Point
Runs feature calculator and publisher for 5s and 10s resolutions
"""
import asyncio
import signal

from services.market.features.feature_calculator import FeatureCalculator
from services.market.features.feature_publisher import FeaturePublisher
from libs.logger import get_logger, setup_logging
from libs.config import config

logger = get_logger(__name__)


class FeatureService:
    """
    Feature service coordinator

    Manages:
    - Feature calculators for 5s and 10s
    - Feature publishers for 5s and 10s
    """

    def __init__(self):
        """Initialize feature service"""
        # Feature calculators
        self.calc_5s = FeatureCalculator(
            resolution="5s",
            horizon_minutes=15,
            waterline_delay_sec=2,
            poll_interval_sec=5.0
        )

        self.calc_10s = FeatureCalculator(
            resolution="10s",
            horizon_minutes=15,
            waterline_delay_sec=2,
            poll_interval_sec=5.0
        )

        # Feature publishers
        self.pub_5s = FeaturePublisher(
            resolution="5s",
            poll_interval_sec=1.0
        )

        self.pub_10s = FeaturePublisher(
            resolution="10s",
            poll_interval_sec=1.0
        )

        self._running = False

        logger.info("FeatureService initialized")

    async def start(self):
        """Start all feature services"""
        logger.info("Starting FeatureService...")

        # Start calculators
        await self.calc_5s.start()
        await self.calc_10s.start()

        # Start publishers
        await self.pub_5s.start()
        await self.pub_10s.start()

        self._running = True
        logger.info("FeatureService started")

    async def stop(self):
        """Stop all feature services"""
        logger.info("Stopping FeatureService...")

        self._running = False

        # Stop calculators
        await self.calc_5s.stop()
        await self.calc_10s.stop()

        # Stop publishers
        await self.pub_5s.stop()
        await self.pub_10s.stop()

        logger.info("FeatureService stopped")

    async def run(self):
        """Run feature service until interrupted"""
        await self.start()

        # Wait for interrupt
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

        await self.stop()


async def main():
    """Main entry point"""
    setup_logging(level="INFO")

    logger.info("Starting Feature Service...")

    service = FeatureService()

    # Setup signal handlers
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(service.stop())

    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    # Run service
    await service.run()

    logger.info("Feature Service shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
