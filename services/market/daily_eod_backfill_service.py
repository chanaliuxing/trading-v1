"""
DailyEODBackfillService
-----------------------
Scheduled daily backfill for 1d bars AFTER market close.

Runs once per day at configurable time (default 16:10 ET) to backfill the completed trading day.

Key differences from CatchupService:
- Catchup: Continuous gap detection (works for intraday bars with RTH windows)
- EOD Scheduler: One-time daily trigger at fixed time (works for daily bars requiring full 24h)

Usage:
    cfg = DailyEODBackfillConfig(
        enabled=True,
        run_time_et="16:10",
        lookback_days=1,
        symbols=[],  # Empty = use config.universe
        request_topic="md.backfill.request.1d.v1",
    )
    service = DailyEODBackfillService(cfg)
    await service.start()
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional

from aiokafka import AIOKafkaProducer
from zoneinfo import ZoneInfo

from libs.config import config
from libs.logger import get_logger

logger = get_logger(__name__)

NY_TZ = ZoneInfo("America/New_York")


@dataclass
class DailyEODBackfillConfig:
    """Configuration for daily EOD backfill scheduler"""

    enabled: bool = True
    run_time_et: str = "16:10"  # "HH:MM" format in ET
    lookback_days: int = 1  # How many days back to backfill (1 = yesterday only)
    symbols: List[str] = None  # If None, use config.universe
    request_topic: str = "md.backfill.request.1d.v1"
    bar_size: str = "1 day"
    use_rth: bool = True


class DailyEODBackfillService:
    """Scheduler that triggers daily bar backfills after market close"""

    def __init__(self, cfg: Optional[DailyEODBackfillConfig] = None) -> None:
        self.cfg = cfg or DailyEODBackfillConfig()

        # Parse run time
        try:
            hour, minute = map(int, self.cfg.run_time_et.split(":"))
            self.run_time = time(hour, minute)
        except Exception as e:
            logger.error(f"Invalid run_time_et format: {self.cfg.run_time_et}", error=str(e))
            self.run_time = time(16, 10)  # Default fallback

        kafka_cfg = getattr(config, "kafka_config", {}) or {}
        self.bootstrap = kafka_cfg.get("bootstrap_servers")
        self.producer: Optional[AIOKafkaProducer] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        logger.info(
            "DailyEODBackfillService initialized",
            run_time_et=self.cfg.run_time_et,
            lookback_days=self.cfg.lookback_days,
            topic=self.cfg.request_topic,
            bar_size=self.cfg.bar_size,
        )

    async def start(self) -> None:
        """Start the scheduler loop"""
        if not self.cfg.enabled:
            logger.info("DailyEODBackfillService disabled in config, not starting")
            return

        logger.info("DailyEODBackfillService starting...")

        if not self.bootstrap:
            raise RuntimeError("Kafka bootstrap_servers missing in config")

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda v: v.encode("utf-8") if isinstance(v, str) else v,
            linger_ms=5,
        )
        await self.producer.start()

        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop(), name="daily_eod_backfill_scheduler")
        logger.info("DailyEODBackfillService started")

    async def stop(self) -> None:
        """Stop the scheduler"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.producer:
            try:
                await asyncio.wait_for(self.producer.stop(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Daily EOD backfill producer stop timed out (degraded Kafka), forcing close")
            except Exception as e:
                logger.warning("Daily EOD backfill producer stop failed", error=str(e))
        logger.info("DailyEODBackfillService stopped")

    async def _scheduler_loop(self) -> None:
        """Main loop: sleep until run time, then trigger backfill"""
        while self._running:
            try:
                await self._wait_until_run_time()
                if self._running:  # Check again after sleep
                    await self._trigger_daily_backfill()
            except Exception as exc:
                logger.error("Scheduler loop error", error=str(exc), exc_info=True)
                await asyncio.sleep(60)  # Wait 1 min before retrying

    async def _wait_until_run_time(self) -> None:
        """Sleep until next scheduled run time (e.g., 16:10 ET)"""
        now_ny = datetime.now(NY_TZ)
        today_run = datetime.combine(now_ny.date(), self.run_time, tzinfo=NY_TZ)

        if now_ny >= today_run:
            # Already passed today's run time, schedule for tomorrow
            next_run = today_run + timedelta(days=1)
        else:
            # Today's run time hasn't occurred yet
            next_run = today_run

        wait_seconds = (next_run - now_ny).total_seconds()
        logger.info(
            f"Next daily backfill scheduled",
            next_run_et=next_run.isoformat(),
            wait_seconds=int(wait_seconds),
        )

        await asyncio.sleep(wait_seconds)

    async def _trigger_daily_backfill(self) -> None:
        """Request daily bars for completed days (yesterday and optionally further back)"""
        if not self.producer:
            logger.error("Producer not initialized, cannot trigger backfill")
            return

        # Determine symbols to backfill
        symbols = self.cfg.symbols or getattr(config, "universe", [])
        if not symbols:
            logger.warning("No symbols configured for daily backfill, skipping")
            return

        now_ny = datetime.now(NY_TZ)

        # Backfill N days back (default: 1 = yesterday only)
        for days_ago in range(1, self.cfg.lookback_days + 1):
            target_date = now_ny.date() - timedelta(days=days_ago)

            # Check if target_date is a weekday (skip weekends for stocks)
            if target_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
                logger.info(f"Skipping weekend date: {target_date}")
                continue

            # Full calendar day: 00:00:00 → 00:00:00 next day (exactly 86400 sec)
            start_dt = datetime.combine(target_date, time(0, 0, 0), tzinfo=NY_TZ)
            end_dt = datetime.combine(target_date + timedelta(days=1), time(0, 0, 0), tzinfo=NY_TZ)

            start_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.timestamp())

            logger.info(
                f"Triggering daily backfill",
                date=target_date.isoformat(),
                symbols=len(symbols),
                start_ts=start_ts,
                end_ts=end_ts,
                duration_sec=end_ts - start_ts,
            )

            for symbol in symbols:
                try:
                    payload = {
                        "data": {
                            "symbol": symbol,
                            "from_ts": start_ts,
                            "to_ts": end_ts,
                            "bar_size": self.cfg.bar_size,
                            "use_rth": self.cfg.use_rth,
                            "reason": "eod_daily",
                        }
                    }

                    await self.producer.send(
                        self.cfg.request_topic,
                        key=symbol,
                        value=payload,
                    )

                    logger.debug(
                        f"EOD daily backfill requested",
                        symbol=symbol,
                        date=target_date.isoformat(),
                        topic=self.cfg.request_topic,
                    )

                except Exception as exc:
                    logger.error(
                        "Failed to enqueue daily backfill",
                        symbol=symbol,
                        date=target_date.isoformat(),
                        error=str(exc),
                    )

        logger.info(
            f"Daily backfill batch completed",
            symbols=len(symbols),
            lookback_days=self.cfg.lookback_days,
        )

    async def trigger_manual_backfill(self, date: datetime.date) -> None:
        """Manually trigger backfill for a specific date (useful for testing/catchup)"""
        symbols = self.cfg.symbols or getattr(config, "universe", [])
        if not symbols:
            logger.warning("No symbols configured for manual backfill")
            return

        start_dt = datetime.combine(date, time(0, 0, 0), tzinfo=NY_TZ)
        end_dt = datetime.combine(date + timedelta(days=1), time(0, 0, 0), tzinfo=NY_TZ)

        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        logger.info(
            f"Manual daily backfill triggered",
            date=date.isoformat(),
            symbols=len(symbols),
        )

        for symbol in symbols:
            payload = {
                "data": {
                    "symbol": symbol,
                    "from_ts": start_ts,
                    "to_ts": end_ts,
                    "bar_size": self.cfg.bar_size,
                    "use_rth": self.cfg.use_rth,
                    "reason": "manual_daily",
                }
            }
            await self.producer.send(self.cfg.request_topic, key=symbol, value=payload)


# Example usage in run_system.py or standalone service
if __name__ == "__main__":
    import sys

    async def main():
        """Standalone runner for testing"""
        cfg = DailyEODBackfillConfig(
            enabled=True,
            run_time_et=sys.argv[1] if len(sys.argv) > 1 else "16:10",
            lookback_days=int(sys.argv[2]) if len(sys.argv) > 2 else 1,
            symbols=[],  # Use config.universe
        )

        service = DailyEODBackfillService(cfg)
        await service.start()

        # Keep running until Ctrl+C
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            await service.stop()

    asyncio.run(main())
