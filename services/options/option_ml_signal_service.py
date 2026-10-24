"""
Option ML Signal Service

Wraps the intraday option ML signal loop with start/stop hooks so it can be
registered in the SystemManager or an external scheduler.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.logger import get_logger, configure_logging
from services.options.ml_intraday_signal_service import run_once

logger = get_logger(__name__)


class OptionMLSignalService:
    def __init__(self, *, poll_interval_sec: float = 60.0, strategy_name: str = "intraday_options_long") -> None:
        self.poll_interval_sec = poll_interval_sec
        self.strategy_name = strategy_name
        self._running = False

    async def start(self) -> None:
        logger.info("Starting OptionMLSignalService", poll_interval_sec=self.poll_interval_sec)
        self._running = True

    async def stop(self) -> None:
        logger.info("Stopping OptionMLSignalService")
        self._running = False

    async def run(self) -> None:
        await self.start()
        try:
            while self._running:
                # Offload blocking signal generation to thread
                await asyncio.to_thread(run_once)
                await asyncio.sleep(self.poll_interval_sec)
        except asyncio.CancelledError:
            pass
        await self.stop()


async def main() -> None:
    configure_logging(level="INFO")
    service = OptionMLSignalService()

    loop = asyncio.get_running_loop()

    def signal_handler() -> None:
        asyncio.create_task(service.stop())

    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
