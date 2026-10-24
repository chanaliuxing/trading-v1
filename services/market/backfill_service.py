"""Backfill service for repairing bars via IBKR historical data."""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from zoneinfo import ZoneInfo

try:
    from ib_insync import IB, BarData, Stock, Index
except Exception:  # pragma: no cover - ib_insync optional at import time
    IB = object  # type: ignore
    BarData = object  # type: ignore
    Stock = object  # type: ignore
    Index = object  # type: ignore

from libs.config import config
from libs.logger import get_logger
from services.ibkr.client_id_registry import get_client_id_registry

logger = get_logger(__name__)


# Kafka topics
GAP_TOPIC = "md.gap.v1"
REQUEST_TOPIC = "md.backfill.request.v1"
BAR_TOPIC = "md.bar.5s.v2"

# Timezone helpers
NY_TZ = ZoneInfo("America/New_York")


def _now_us() -> int:
    return int(time.time() * 1_000_000)


@dataclass(order=True)
class BackfillJob:
    """Work item describing a historical repair request."""

    priority: int
    symbol: str = field(compare=False)
    from_ts: int = field(compare=False)  # inclusive, seconds
    to_ts: int = field(compare=False)  # exclusive, seconds
    use_rth: bool = field(compare=False, default=True)
    reason: str = field(compare=False, default="gap")
    bar_size: str = field(compare=False, default="5 secs")


class BackfillService:
    """Consumes gap events, fetches historical bars from IBKR, writes back to Kafka."""

    def __init__(
        self,
        ib: Optional[IB] = None,
        *,
        kafka_bootstrap: Optional[str] = None,
        redis_url: Optional[str] = None,
        ib_host: Optional[str] = None,
        ib_port: Optional[int] = None,
        ib_client_id: Optional[int] = None,
        gap_topic: str = GAP_TOPIC,
        request_topic: Optional[str] = REQUEST_TOPIC,
        bar_topic: str = BAR_TOPIC,
        group_id: str = "backfill.v1",
        max_ib_concurrency: int = 2,
        min_ib_interval_sec: float = 1.0,
        default_use_rth: bool = True,
        default_bar_size: str = "5 secs",
        emit_quality: str = "BACKFILL",
        margin_seconds: int = 10,
        worker_count: int = 2,
        shutdown_event: Optional[asyncio.Event] = None,
    ) -> None:
        self.ib: Optional[IB] = ib
        self._uses_shared_market_ib = ib is None
        self._market_handle_epoch: Optional[int] = None
        self._owns_ib = False

        kafka_cfg = getattr(config, "kafka_config", {}) or {}
        self.bootstrap = kafka_bootstrap or kafka_cfg.get("bootstrap_servers")

        ib_cfg = config.ib_config
        self._ib_host = ib_host or ib_cfg.get("host", "127.0.0.1")
        self._ib_port = int(ib_port or ib_cfg.get("port", 4002))
        # If ib_client_id is explicitly provided, use it directly; otherwise use registry.
        if ib_client_id is not None:
            self._ib_client_id = int(ib_client_id)
        else:
            self._ib_client_id = int(get_client_id_registry().resolve("backfill"))

        self.gap_topic = gap_topic
        self.request_topic = request_topic
        self.bar_topic = bar_topic
        self.group_id = group_id
        self.default_use_rth = default_use_rth
        self.default_bar_size = default_bar_size
        self.emit_quality = emit_quality
        self.margin_seconds = max(0, margin_seconds)
        self.worker_count = max(1, worker_count)

        self.consumer: Optional[AIOKafkaConsumer] = None
        self.producer: Optional[AIOKafkaProducer] = None

        self._jobs: "asyncio.PriorityQueue[BackfillJob]" = asyncio.PriorityQueue()
        self._workers: List[asyncio.Task] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._worker_monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._shutdown_event = shutdown_event
        self._connect_on_start = str(config.get("services.backfill.connect_on_start", "false")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        # pacing controls
        self._ib_semaphore = asyncio.Semaphore(max(1, max_ib_concurrency))
        self._min_ib_interval = float(min_ib_interval_sec)
        self._last_ib_request = 0.0

        # dedup within process
        self._emitted_keys: set[Tuple[str, int]] = set()

        logger.info(
            "BackfillService initialized",
            kafka_bootstrap=self.bootstrap,
            redis_url=redis_url,
            gap_topic=self.gap_topic,
            request_topic=self.request_topic,
            bar_topic=self.bar_topic,
            max_ib_concurrency=max_ib_concurrency,
            min_ib_interval_sec=min_ib_interval_sec,
            ib_host=self._ib_host,
            ib_port=self._ib_port,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info("BackfillService starting...")

        if not self.bootstrap:
            raise RuntimeError("Kafka bootstrap servers not configured")

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda v: v.encode("utf-8") if isinstance(v, str) else v,
            request_timeout_ms=10000  # 10s timeout for requests (valid for aiokafka)
        )
        await self.producer.start()

        topics = [self.gap_topic]
        if self.request_topic:
            topics.append(self.request_topic)

        self.consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self.bootstrap,
            group_id=self.group_id,
            auto_offset_reset="latest",
            enable_auto_commit=True,
            auto_commit_interval_ms=5000,  # Commit every 5 seconds
            session_timeout_ms=90000,  # 90s session timeout to tolerate IB stalls
            heartbeat_interval_ms=9000,  # Heartbeat every 9s
            max_poll_interval_ms=300000,  # 5 minutes max processing time between polls
            max_poll_records=50,  # Limit batch size to avoid long processing
            request_timeout_ms=180000,  # Kafka request timeout aligned with lake writer
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            key_deserializer=lambda v: v.decode("utf-8") if v else None,
        )
        await self.consumer.start()

        self._running = True
        
        if self._connect_on_start:
            # Optional warm connection. Disabled by default to avoid many idle
            # BackfillService instances occupying IBKR client sessions.
            asyncio.create_task(self._connect_ib())
        
        asyncio.create_task(self._consume_loop())

        for idx in range(self.worker_count):
            task = asyncio.create_task(self._worker_loop(idx))
            self._workers.append(task)

        # Start connection monitoring only for dedicated sessions; shared market IB
        # is already monitored by the canonical connection manager.
        if self._owns_ib:
            # Monitor task will be started after IB object is created in _connect_ib
            pass

        # Start worker health monitoring (auto-restart crashed workers)
        self._worker_monitor_task = asyncio.create_task(self._monitor_workers())
        logger.info("BackfillService worker health monitoring enabled")

        logger.info("BackfillService started", workers=len(self._workers))

    async def _connect_ib(self) -> None:
        """Connect to IBKR in background."""
        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            if self._uses_shared_market_ib:
                handle = await get_ib_connection_manager().get_market_ib_handle(
                    host=self._ib_host,
                    port=self._ib_port,
                )
                self.ib = handle.ib
                self._market_handle_epoch = int(handle.epoch)
                logger.info(
                    "BackfillService attached to shared market IB",
                    client_id=self._ib_client_id,
                    market_epoch=self._market_handle_epoch,
                )
                return

            if isinstance(self.ib, IB) and self.ib.isConnected():
                logger.info("BackfillService connected to IBKR", client_id=self._ib_client_id)
        except Exception as e:
            logger.warning("BackfillService initial IBKR connection failed (workers will retry)", error=str(e))

    async def stop(self) -> None:
        logger.info("BackfillService stopping...")
        self._running = False

        # Cancel worker monitor task
        if self._worker_monitor_task and not self._worker_monitor_task.done():
            self._worker_monitor_task.cancel()
            try:
                await self._worker_monitor_task
            except asyncio.CancelledError:
                pass

        # Cancel connection monitor task
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # Cancel worker tasks
        for task in self._workers:
            task.cancel()
        self._workers.clear()

        # Commit consumer offsets before stopping to avoid UnknownMemberIdError
        if self.consumer:
            try:
                await asyncio.wait_for(self.consumer.commit(), timeout=2.0)
                logger.debug("Consumer offsets committed", group_id=self.group_id)
            except asyncio.TimeoutError:
                logger.warning("Consumer offset commit timed out", group_id=self.group_id)
            except Exception as exc:
                logger.debug("Consumer offset commit skipped", group_id=self.group_id, error=str(exc))

            try:
                await asyncio.wait_for(self.consumer.stop(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Consumer stop timed out", group_id=self.group_id)
            except Exception as exc:
                logger.warning("Consumer stop failed", group_id=self.group_id, error=str(exc))
            self.consumer = None

        # Stop producer
        if self.producer:
            try:
                await asyncio.wait_for(self.producer.stop(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Backfill producer stop timed out (degraded Kafka), forcing close")
            except Exception as exc:
                logger.warning("Backfill producer stop failed", error=str(exc))
            self.producer = None

        # Disconnect IB if we own it
        if self._owns_ib and isinstance(self.ib, IB) and self.ib.isConnected():
            self.ib.disconnect()

        logger.info("BackfillService stopped")

    # ------------------------------------------------------------------
    # external scheduling API
    # ------------------------------------------------------------------

    async def schedule_job(
        self,
        symbol: str,
        from_ts: int,
        to_ts: int,
        *,
        priority: int = 100,
        use_rth: Optional[bool] = None,
        reason: str = "manual",
        bar_size: Optional[str] = None,
    ) -> None:
        job = BackfillJob(
            priority=priority,
            symbol=symbol,
            from_ts=int(from_ts),
            to_ts=int(to_ts),
            use_rth=self.default_use_rth if use_rth is None else bool(use_rth),
            reason=reason,
            bar_size=bar_size or self.default_bar_size,
        )
        await self._jobs.put(job)
        logger.info("Job scheduled", symbol=symbol, from_ts=from_ts, to_ts=to_ts, priority=priority, reason=reason)

    # ------------------------------------------------------------------
    # Kafka consumption
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        assert self.consumer is not None
        async for msg in self.consumer:
            # Check shutdown event
            if not self._running or (self._shutdown_event and self._shutdown_event.is_set()):
                logger.info("Shutdown detected in consume loop, exiting...")
                break

            try:
                payload = msg.value or {}
                data = payload.get("data") if isinstance(payload, dict) else None
                d = data or payload

                symbol = d.get("symbol")
                if not symbol:
                    continue

                from_ts = d.get("from_ts") or d.get("last_bar_ts")
                to_ts = d.get("to_ts") or d.get("now_ts")
                if from_ts is None or to_ts is None:
                    continue

                if int(to_ts) <= int(from_ts):
                    continue

                from_ts = max(0, int(from_ts) - self.margin_seconds)
                to_ts = int(to_ts)

                # Honor request-specific settings when provided; fall back to defaults
                reason = d.get("reason", "gap" if msg.topic == self.gap_topic else "manual")
                req_use_rth = d.get("use_rth", self.default_use_rth)
                req_bar_size = d.get("bar_size", self.default_bar_size)

                # DEBUG: Log the bar_size value to identify any corruption
                logger.info(
                    "Consume message bar_size",
                    symbol=symbol,
                    bar_size_raw=req_bar_size,
                    bar_size_repr=repr(req_bar_size),
                    bar_size_type=type(req_bar_size).__name__,
                )

                job = BackfillJob(
                    priority=10 if reason == "gap" else 50,
                    symbol=symbol,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    use_rth=bool(req_use_rth),
                    reason=reason,
                    bar_size=str(req_bar_size),
                )
                await self._jobs.put(job)
                logger.info(
                    "Gap job queued",
                    symbol=symbol,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    reason=reason,
                    bar_size=job.bar_size,
                    use_rth=job.use_rth,
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error("Gap consume error", error=str(exc))

    # ------------------------------------------------------------------
    # Worker pipeline
    # ------------------------------------------------------------------

    async def _worker_loop(self, worker_idx: int) -> None:
        """Worker loop with automatic IBKR reconnection on connection loss."""
        logger.info(f"Worker {worker_idx} started")

        while self._running and (not self._shutdown_event or not self._shutdown_event.is_set()):
            job = None
            try:
                # Use timeout to allow periodic shutdown checks
                job = await asyncio.wait_for(self._jobs.get(), timeout=1.0)

                # Check IBKR connection before processing job
                if not isinstance(self.ib, IB) or not self.ib.isConnected():
                    logger.warning(
                        f"Worker {worker_idx}: IBKR disconnected, attempting reconnect...",
                        worker=worker_idx,
                        job_symbol=job.symbol if job else None,
                    )

                    # Attempt reconnection with exponential backoff
                    reconnected = await self._attempt_reconnect(worker_idx)

                    if not reconnected:
                        # Reconnection failed - put job back to queue and continue
                        logger.error(
                            f"Worker {worker_idx}: Reconnect failed, requeueing job",
                            worker=worker_idx,
                            job_symbol=job.symbol if job else None,
                        )
                        if job:
                            await self._jobs.put(job)
                        await asyncio.sleep(5)  # Wait before next attempt
                        continue

                # Process job
                await self._process_job(job)

            except asyncio.TimeoutError:
                # Timeout is normal - allows checking shutdown event
                continue
            except asyncio.CancelledError:
                logger.info(f"Worker {worker_idx} cancelled")
                break
            except Exception as exc:
                # Log error but don't crash - continue processing other jobs
                logger.error(
                    "Backfill worker error",
                    worker=worker_idx,
                    error=str(exc),
                    job_symbol=job.symbol if job else None,
                    exc_info=True,
                )
                # Brief sleep to avoid tight error loop
                await asyncio.sleep(1)
            finally:
                if job is not None:
                    self._jobs.task_done()

        logger.info(f"Worker {worker_idx} exiting")

    async def _attempt_reconnect(self, worker_idx: int, max_retries: int = 3) -> bool:
        """
        Attempt to reconnect to IBKR with exponential backoff.

        Args:
            worker_idx: Worker ID for logging
            max_retries: Maximum number of reconnection attempts

        Returns:
            True if reconnected successfully, False otherwise
        """
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    f"Worker {worker_idx}: Reconnection attempt {attempt}/{max_retries}",
                    worker=worker_idx,
                    attempt=attempt,
                    max_retries=max_retries,
                )

                from services.ibkr.ib_connection_manager import get_ib_connection_manager
                if self._uses_shared_market_ib:
                    handle = await get_ib_connection_manager().get_market_ib_handle(
                        host=self._ib_host,
                        port=self._ib_port,
                    )
                    self.ib = handle.ib
                    self._market_handle_epoch = int(handle.epoch)
                else:
                    if not isinstance(self.ib, IB):
                        logger.error(f"Worker {worker_idx}: IB client not available for reconnection")
                        return False
                    if self.ib.isConnected():
                        self.ib.disconnect()
                        await asyncio.sleep(1)
                    await self._connect_ib()

                if self.ib.isConnected():
                    logger.info(
                        f"✅ Worker {worker_idx}: Reconnected to IBKR",
                        worker=worker_idx,
                        attempt=attempt,
                        host=self._ib_host,
                        port=self._ib_port,
                    )
                    return True

            except Exception as e:
                logger.warning(
                    f"Worker {worker_idx}: Reconnection attempt {attempt} failed",
                    worker=worker_idx,
                    attempt=attempt,
                    error=str(e),
                )

                # Exponential backoff: 2s, 4s, 8s
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    await asyncio.sleep(wait_time)

        logger.error(
            f"🔴 Worker {worker_idx}: All reconnection attempts failed",
            worker=worker_idx,
            max_retries=max_retries,
        )
        return False

    async def _connection_monitor(self) -> None:
        """
        Lightweight connection health monitor.
        Does NOT attempt reconnection - only logs status for alerting.
        Runs only when BackfillService owns the IB connection.
        """
        check_interval = 60  # Check every 60 seconds
        consecutive_failures = 0
        last_status = None

        logger.info("BackfillService connection monitor started", interval_sec=check_interval)

        while self._running and not (self._shutdown_event and self._shutdown_event.is_set()):
            try:
                await asyncio.sleep(check_interval)

                if not isinstance(self.ib, IB):
                    continue

                is_connected = self.ib.isConnected()
                current_status = "connected" if is_connected else "disconnected"

                # Only log on status change or periodic heartbeat
                if current_status != last_status:
                    if is_connected:
                        logger.info(
                            "✅ BackfillService IBKR connection restored",
                            alert_type="ibkr_connected",
                            downtime_checks=consecutive_failures,
                        )
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        logger.error(
                            "🔴 BackfillService IBKR connection lost",
                            alert_type="ibkr_disconnected",
                            consecutive_failures=consecutive_failures,
                            action_required="Manual restart of run_system.py recommended",
                        )
                    last_status = current_status
                elif not is_connected:
                    # Still disconnected - increment counter and log periodically
                    consecutive_failures += 1
                    if consecutive_failures % 5 == 0:  # Every 5 checks (5 minutes)
                        logger.error(
                            "🔴 BackfillService IBKR still disconnected",
                            alert_type="ibkr_disconnected",
                            consecutive_failures=consecutive_failures,
                            minutes_down=consecutive_failures * (check_interval / 60),
                        )
                elif is_connected and consecutive_failures == 0:
                    # Periodic heartbeat when healthy (every 10 checks = 10 minutes)
                    if (int(time.time()) // check_interval) % 10 == 0:
                        logger.debug(
                            "BackfillService IBKR connection healthy",
                            uptime_checks=1,
                        )

            except asyncio.CancelledError:
                logger.info("BackfillService connection monitor cancelled")
                break
            except Exception as e:
                logger.error("Connection monitor error", error=str(e))
                await asyncio.sleep(check_interval)

        logger.info("BackfillService connection monitor stopped")

    async def _monitor_workers(self) -> None:
        """
        Monitor worker health and auto-restart crashed workers.
        Encapsulates worker supervision within BackfillService.
        """
        check_interval = 10  # Check every 10 seconds
        logger.info("BackfillService worker monitor started", interval_sec=check_interval)

        while self._running and not (self._shutdown_event and self._shutdown_event.is_set()):
            try:
                await asyncio.sleep(check_interval)

                # Check each worker task
                for idx, task in enumerate(self._workers):
                    if task.done():
                        # Worker crashed or exited abnormally
                        exc = None
                        try:
                            exc = task.exception()
                        except asyncio.CancelledError:
                            # Normal cancellation during shutdown
                            continue

                        # Don't restart workers if shutdown is in progress
                        if self._shutdown_event and self._shutdown_event.is_set():
                            logger.debug(f"Worker {idx} exited during shutdown, not restarting", worker=idx)
                            continue

                        if exc:
                            logger.error(
                                f"🔴 Worker {idx} crashed, restarting...",
                                worker=idx,
                                error=str(exc),
                                exception_type=type(exc).__name__,
                            )
                        else:
                            logger.warning(
                                f"⚠️ Worker {idx} exited unexpectedly, restarting...",
                                worker=idx,
                            )

                        # Restart worker
                        new_task = asyncio.create_task(self._worker_loop(idx))
                        self._workers[idx] = new_task
                        logger.info(f"✅ Worker {idx} restarted", worker=idx)

            except asyncio.CancelledError:
                logger.info("BackfillService worker monitor cancelled")
                break
            except Exception as e:
                logger.error("Worker monitor error", error=str(e), exc_info=True)
                await asyncio.sleep(check_interval)

        logger.info("BackfillService worker monitor stopped")

    async def _process_job(self, job: BackfillJob) -> None:
        if job.to_ts <= job.from_ts:
            return

        symbol = job.symbol
        logger.info(
            "Backfill start",
            symbol=symbol,
            from_ts=job.from_ts,
            to_ts=job.to_ts,
            reason=job.reason,
            bar_size=job.bar_size,
        )

        for seg_start, seg_end in self._segment_range(job.from_ts, job.to_ts, job.bar_size):
            # Check shutdown before processing each segment
            if self._shutdown_event and self._shutdown_event.is_set():
                logger.warning("Shutdown initiated during backfill, aborting job", symbol=symbol)
                return

            try:
                bars = await self._fetch_ib_bars(
                    symbol=symbol,
                    seg_start=seg_start,
                    seg_end=seg_end,
                    bar_size=job.bar_size,
                    use_rth=job.use_rth,
                )
                logger.info("Fetched bars", symbol=symbol, bar_size=job.bar_size, count=len(bars), seg_start=seg_start, seg_end=seg_end)
                await self._emit_bars(symbol, bars, job.bar_size)
            except Exception as exc:
                delay = random.uniform(1.0, 3.0)

                # MONITORING: Detect if failure is due to IBKR disconnection
                error_msg = str(exc).lower()
                is_connection_error = (
                    "connection" in error_msg or
                    "disconnect" in error_msg or
                    "not connected" in error_msg or
                    "connection lost" in error_msg
                )

                if is_connection_error:
                    logger.error(
                        "🔴 Backfill segment failed - IBKR connection issue detected",
                        symbol=symbol,
                        seg_start=seg_start,
                        seg_end=seg_end,
                        error=str(exc),
                        alert_type="backfill_connection_error",
                        ibkr_connected=self.ib.isConnected() if isinstance(self.ib, IB) else False,
                        sleep=delay,
                    )
                else:
                    logger.warning(
                        "Backfill segment failed",
                        symbol=symbol,
                        seg_start=seg_start,
                        seg_end=seg_end,
                        error=str(exc),
                        sleep=delay,
                    )

                await asyncio.sleep(delay)
                continue

        logger.info("Backfill finished", symbol=symbol, from_ts=job.from_ts, to_ts=job.to_ts)

    # ------------------------------------------------------------------
    # IBKR helpers
    # ------------------------------------------------------------------

    async def _pace_ib(self) -> None:
        now = time.time()
        wait = self._last_ib_request + self._min_ib_interval - now
        if wait > 0:
            await asyncio.sleep(wait + random.uniform(0.0, 0.2))
        self._last_ib_request = time.time()

    async def _fetch_ib_bars(
        self,
        *,
        symbol: str,
        seg_start: int,
        seg_end: int,
        bar_size: str,
        use_rth: bool,
    ) -> List[BarData]:
        if not isinstance(self.ib, IB):
            raise RuntimeError("IB client not available")

        # MONITORING: Check IBKR connection status before request
        if not self.ib.isConnected():
            logger.warning(
                "BackfillService IBKR disconnected, skipping fetch (will retry)",
                symbol=symbol,
                bar_size=bar_size,
                service_name=self.__class__.__name__,
                alert_type="ibkr_disconnected",
                action="Worker will attempt reconnection before next job",
            )
            # Return empty list instead of raising - worker loop will handle reconnection
            return []

        # CRITICAL: Skip partial-day requests for daily bars
        # IBKR requires full day durations for "1 day" bar size
        duration_seconds = seg_end - seg_start
        if bar_size in ("1 day", "1 d"):
            # Daily bars require at least 1 full day (86400 seconds)
            if duration_seconds < 86400:
                logger.warning(
                    "Skipping partial-day request for daily bars",
                    symbol=symbol,
                    duration_seconds=duration_seconds,
                    bar_size=bar_size,
                )
                return []

        async with self._ib_semaphore:
            await self._pace_ib()

            # Create contract - handle VIX as Index
            if symbol == 'VIX':
                contract = Index('VIX', 'CBOE', 'USD')
            else:
                contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)

            end_dt = datetime.fromtimestamp(seg_end, tz=NY_TZ)
            span_seconds = max(1, seg_end - seg_start)
            bar_seconds = self._bar_size_to_seconds(bar_size)

            # Use day-based duration for bar sizes >= 1 minute (more efficient)
            if bar_seconds >= 60 and (span_seconds % 86400 == 0 or span_seconds >= 86400):
                days = max(1, span_seconds // 86400)
                duration_str = f"{days} D"
            else:
                duration_str = f"{span_seconds} S"

            logger.info(
                "reqHistoricalData CALL",
                symbol=symbol,
                end=end_dt.isoformat(),
                duration=duration_str,
                barSize=bar_size,
                bar_size_repr=repr(bar_size),
                bar_size_len=len(bar_size),
                use_rth=use_rth,
            )

            bars = await self.ib.reqHistoricalDataAsync(
                contract=contract,
                endDateTime=end_dt,
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=use_rth,
                formatDate=2,
                keepUpToDate=False,
            )

        if not bars:
            logger.debug("IBKR returned no historical data for chunk", symbol=symbol, start=seg_start, end=seg_end, bar_size=bar_size)

        return list(bars or [])

    # ------------------------------------------------------------------
    # Kafka emission
    # ------------------------------------------------------------------

    async def _emit_bars(self, symbol: str, bars: List[BarData], bar_size: str) -> None:
        if not bars or self.producer is None:
            return

        bar_seconds = max(self._bar_size_to_seconds(bar_size), 1)

        logger.info("Emit context", bar_size=bar_size, bar_seconds=bar_seconds, rows=len(bars))
        for i, bar in enumerate(bars):
            # Yield control every 100 bars to prevent blocking the event loop
            # This allows background tasks like Kafka heartbeats to run.
            if i > 0 and i % 100 == 0:
                await asyncio.sleep(0)

            dt = getattr(bar, "date", None)
            # Normalize IBKR date to timezone-aware NY datetime
            if isinstance(dt, str):
                s = dt.strip()
                parsed = None
                # Try ISO first
                try:
                    parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
                except Exception:
                    parsed = None
                # Try common IBKR formats
                if parsed is None:
                    try:
                        parsed = datetime.strptime(s, "%Y%m%d %H:%M:%S")
                    except Exception:
                        parsed = None
                if parsed is None:
                    try:
                        parsed = datetime.strptime(s, "%Y%m%d")
                    except Exception:
                        parsed = None
                if parsed is None:
                    logger.warning("Unrecognized IBKR bar date format", date=s)
                    continue
                dt = parsed

            if not isinstance(dt, datetime):
                continue
            if dt.tzinfo is None:
                # IB often returns naive datetime in New York local time.
                # With ZoneInfo, attach the tz directly.
                dt = dt.replace(tzinfo=NY_TZ)
            else:
                # Convert to NY timezone if different
                try:
                    dt = dt.astimezone(NY_TZ)
                except Exception:
                    pass

            ts_start = int(dt.timestamp())
            ts_end = ts_start + bar_seconds

            key = (symbol, ts_start)
            if key in self._emitted_keys:
                continue
            self._emitted_keys.add(key)
            if len(self._emitted_keys) > 200_000:
                self._emitted_keys.pop()

            payload = {
                "schema_v": 2,
                "metadata": {
                    "ts_us": _now_us(),
                    "producer": "backfill",
                },
                "data": {
                    "symbol": symbol,
                    "ts_start_us": ts_start * 1_000_000,
                    "ts_end_us": ts_end * 1_000_000,
                    "open": float(getattr(bar, "open", 0.0)),
                    "high": float(getattr(bar, "high", 0.0)),
                    "low": float(getattr(bar, "low", 0.0)),
                    "close": float(getattr(bar, "close", 0.0)),
                    "volume": float(getattr(bar, "volume", 0.0)),
                    "vwap": float(getattr(bar, "close", 0.0)),
                    "trades": 0,
                    "quality": self.emit_quality,
                    "is_backfill": 1,
                },
            }

            try:
                if bar_seconds >= 86400:
                    logger.info("Emitting daily bar", symbol=symbol, ts_start=ts_start, close=float(getattr(bar, 'close', 0.0)), volume=float(getattr(bar, 'volume', 0.0)))

                # Add detailed timing logs to track Kafka send performance
                send_start = time.time()
                logger.debug("Send bar start", topic=self.bar_topic, symbol=symbol, ts_start_us=payload['data']['ts_start_us'], producer_client_id=id(self.producer))

                # Send with explicit await to catch timeout
                future = await self.producer.send(self.bar_topic, key=symbol, value=payload)

                send_duration_ms = (time.time() - send_start) * 1000
                logger.debug("Send bar done", topic=self.bar_topic, symbol=symbol, ts_start_us=payload['data']['ts_start_us'], duration_ms=int(send_duration_ms))

                # Log if send took unusually long (potential sign of connection issues)
                if send_duration_ms > 1000:
                    logger.warning("Slow Kafka send detected", symbol=symbol, duration_ms=int(send_duration_ms), topic=self.bar_topic)

            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Emit bar failed", symbol=symbol, error=str(exc), error_type=type(exc).__name__, producer_connected=self.producer is not None)
    def _segment_range(self, start_ts: int, end_ts: int, bar_size: str) -> List[Tuple[int, int]]:
        """Generate optimal time segments for IBKR historical data requests.

        Uses large day-based chunks for bar sizes >= 1 minute to minimize API calls
        and maximize throughput (similar to load_hist_multi.py strategy).
        """
        if end_ts <= start_ts:
            return []

        bar_seconds = max(self._bar_size_to_seconds(bar_size), 1)

        # For 1-minute+ bars, use large day-based chunks (like load_hist_multi.py)
        if bar_seconds >= 60:
            # Define max days per request based on bar size (conservative IBKR limits)
            if bar_size == "1 min":
                max_days_per_req = 30  # Safe for 1-min bars
            elif bar_size in ("2 mins", "3 mins"):
                max_days_per_req = 60
            elif bar_size in ("5 mins", "10 mins", "15 mins", "30 mins"):
                max_days_per_req = 365  # IBKR allows up to 365 days for these
            elif bar_size in ("1 hour", "2 hours", "3 hours", "4 hours"):
                max_days_per_req = 365
            elif bar_size in ("1 day", "1 d"):
                max_days_per_req = 365
            else:
                max_days_per_req = 30  # Conservative default

            start_dt = datetime.fromtimestamp(start_ts, tz=NY_TZ)
            end_dt = datetime.fromtimestamp(end_ts, tz=NY_TZ)

            segments: List[Tuple[int, int]] = []
            cursor_dt = start_dt
            while cursor_dt < end_dt:
                seg_end_dt = min(cursor_dt + timedelta(days=max_days_per_req), end_dt)
                segments.append((
                    int(cursor_dt.timestamp()),
                    int(seg_end_dt.timestamp()),
                ))
                cursor_dt = seg_end_dt

            logger.debug(
                "Day-based segmentation",
                bar_size=bar_size,
                max_days_per_req=max_days_per_req,
                num_segments=len(segments),
            )
            return segments

        # For sub-minute bars (5 secs, 15 secs), keep smaller chunks
        else:
            chunk_seconds = 30 * 60  # 30 minutes for sub-minute bars
            chunk_seconds = max(bar_seconds, (chunk_seconds // bar_seconds) * bar_seconds)

            segments: List[Tuple[int, int]] = []
            cursor = start_ts
            while cursor < end_ts:
                seg_end = min(cursor + chunk_seconds, end_ts)
                segments.append((cursor, seg_end))
                cursor = seg_end

            return segments

    @staticmethod
    def _bar_size_to_seconds(bar_size: str) -> int:
        size = bar_size.strip().lower()
        lookup = {
            "1 sec": 1,
            "5 secs": 5,
            "10 secs": 10,
            "15 secs": 15,
            "30 secs": 30,
            "1 min": 60,
            "2 mins": 120,
            "3 mins": 180,
            "5 mins": 300,
            "10 mins": 600,
            "15 mins": 900,
            "30 mins": 1800,
            "1 hour": 3600,
            "2 hours": 7200,
            "4 hours": 14400,
            "1 day": 86400,
            "1 week": 604800
        }
        if size in lookup:
            return lookup[size]
        try:
            number, unit = size.split()
            value = float(number)
            if unit.startswith("sec"):
                return int(value)
            if unit.startswith("min"):
                return int(value * 60)
            if unit.startswith("hour"):
                return int(value * 3600)
            if unit.startswith("day"):
                return int(value * 86400)
            if unit.startswith("week"):
                return int(value * 604800)
        except Exception:
            pass
        return 60
