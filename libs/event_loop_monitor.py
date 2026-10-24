"""
Event Loop Monitoring Utility
Detects event loop blocking/starvation that can prevent Kafka clients from functioning
"""
import asyncio
import time
from typing import Optional

from libs.logger import get_logger

logger = get_logger(__name__)


class EventLoopMonitor:
    """
    Monitors asyncio event loop lag to detect blocking operations.

    High lag (>100ms) indicates the event loop is blocked, which prevents
    aiokafka background tasks (heartbeats, send retries) from running.
    """

    def __init__(self, check_interval_sec: float = 0.5, warn_threshold_ms: float = 200):
        """
        Initialize event loop monitor.

        Args:
            check_interval_sec: How often to check loop lag
            warn_threshold_ms: Log warning if lag exceeds this threshold
        """
        self.check_interval_sec = check_interval_sec
        self.warn_threshold_ms = warn_threshold_ms
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start monitoring loop lag"""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("EventLoopMonitor started",
                   check_interval_sec=self.check_interval_sec,
                   warn_threshold_ms=self.warn_threshold_ms)

    async def stop(self):
        """Stop monitoring"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("EventLoopMonitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop"""
        loop = asyncio.get_running_loop()
        last_time = loop.time()

        while self._running:
            try:
                await asyncio.sleep(self.check_interval_sec)

                now = loop.time()
                actual_elapsed = now - last_time
                expected_elapsed = self.check_interval_sec
                lag_ms = (actual_elapsed - expected_elapsed) * 1000

                if lag_ms > self.warn_threshold_ms:
                    logger.warning("Event loop lag detected",
                                 lag_ms=int(lag_ms),
                                 check_interval_sec=self.check_interval_sec,
                                 message="Event loop may be blocked by sync/CPU-heavy operations")

                # Log periodic health check
                if int(now) % 60 == 0:  # Every minute
                    logger.debug("Event loop health", lag_ms=int(lag_ms))

                last_time = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Event loop monitor error", error=str(e))
                await asyncio.sleep(1)


# Global instance
_monitor: Optional[EventLoopMonitor] = None


async def start_monitoring(check_interval_sec: float = 0.5, warn_threshold_ms: float = 200):
    """Start global event loop monitoring"""
    global _monitor
    if _monitor is None:
        _monitor = EventLoopMonitor(check_interval_sec, warn_threshold_ms)
    await _monitor.start()


async def stop_monitoring():
    """Stop global event loop monitoring"""
    global _monitor
    if _monitor:
        await _monitor.stop()
        _monitor = None
