# services/reconciliation/scheduled_reconciler.py
"""
Scheduled Reconciler Service

Runs the offline data reconciliation process at a configured time each day.
"""
import asyncio
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, time

from libs.logger import get_logger
from .offline_data_reconciler import run_reconciliation, NY_TZ

logger = get_logger(__name__)

@dataclass
class ScheduledReconConfig:
    """Configuration for a single scheduled reconciliation job."""
    name: str
    enabled: bool
    run_time: str  # HH:MM format
    res: str
    days: int
    use_rth: bool = True
    symbols: Optional[List[str]] = field(default_factory=list)

class ScheduledReconciler:
    """
    A service that triggers data reconciliation on a daily schedule.
    """
    def __init__(self, config: ScheduledReconConfig):
        self.config = config
        self.run_time_obj: time = datetime.strptime(config.run_time, "%H:%M").time()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ran_today = False
        logger.info("ScheduledReconciler initialized", name=config.name, run_time=config.run_time)

    async def start(self):
        """Starts the scheduler loop."""
        if not self.config.enabled:
            logger.warning("ScheduledReconciler is disabled by config", name=self.config.name)
            return
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info("ScheduledReconciler started", name=self.config.name)

    async def stop(self):
        """Stops the scheduler loop."""
        self._running = False
        if self._task:
            try:
                self._task.cancel()
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ScheduledReconciler stopped", name=self.config.name)

    async def _scheduler_loop(self):
        """The main loop that checks the time and triggers the reconciliation."""
        logger.info("Scheduler loop started", name=self.config.name)
        while self._running:
            now_ny = datetime.now(NY_TZ)

            # Reset the 'ran today' flag just after midnight
            if now_ny.time() < self.run_time_obj and self._ran_today:
                logger.info("Resetting daily run flag for reconciler", name=self.config.name)
                self._ran_today = False

            # Check if it's time to run
            if now_ny.time() >= self.run_time_obj and not self._ran_today:
                logger.info("Triggering scheduled reconciliation", name=self.config.name)
                try:
                    await run_reconciliation(
                        res=self.config.res,
                        days=self.config.days,
                        symbols=self.config.symbols,
                        use_rth=self.config.use_rth
                    )
                    self._ran_today = True
                    logger.info("Scheduled reconciliation finished successfully", name=self.config.name)
                except Exception as e:
                    logger.error("Scheduled reconciliation failed", name=self.config.name, error=str(e), exc_info=True)
            
            # Wait for 60 seconds before checking again
            await asyncio.sleep(60)
