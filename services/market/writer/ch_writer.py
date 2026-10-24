# services/market/writer/ch_writer.py
"""ClickHouse Bar Writer (parameterized)
- Consumes bar messages from Kafka (aiokafka)
- Writes to ClickHouse using ClickHouseDB.insert_async()
- Idempotent via ReplacingMergeTree(ver)
- Minimal assumptions about message schema (data/metadata keys)
- Can optionally filter after-hours bars for intraday resolutions
"""
import asyncio
import json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo

from aiokafka import AIOKafkaConsumer
from aiokafka.structs import ConsumerRecord
from aiokafka.errors import KafkaError, KafkaConnectionError, CommitFailedError

from libs.db import ch_pool  # Use shared global connection pool
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)
NY_TZ = ZoneInfo("America/New_York")

def _us_to_dt(us: Optional[int]) -> Optional[datetime]:
    if us is None:
        return None
    try:
        dt = datetime.fromtimestamp(us / 1_000_000, tz=NY_TZ)
        return dt.replace(tzinfo=None)
    except Exception:
        return None

def _is_rth(dt: datetime) -> bool:
    """Check if datetime is during Regular Trading Hours (9:30 AM - 4:00 PM ET)."""
    if dt is None:
        return False
    # dt is already in local time (America/New_York) from _us_to_dt()
    # Check if weekday (Monday=0, Sunday=6)
    if dt.weekday() >= 5:  # Saturday or Sunday
        return False
    # Check time (9:30 AM - 4:00 PM)
    t = dt.time()
    return time(9, 30) <= t < time(16, 0)

class ClickHouseBarWriter:
    """Generic Kafka→ClickHouse writer for bar messages."""

    # Column order compatible with existing bars_* tables in your ch_init.sql
    # Note: bar_sec and created_at have DEFAULT values in CH, so we don't send them
    COLS: Tuple[str, ...] = (
        "symbol",
        "ts_start",
        "ts_end",
        "open",
        "high",
        "low",
        "close",
        "vwap",
        "volume",
        "trades",
        "bid_last",
        "ask_last",
        "mid_last",
        "ver",
        "is_backfill",
    )

    def __init__(
        self,
        *,
        topic: str = "md.bar.5s.v2",
        table_name: str = "bars_5s",
        group_id: str = "ch_writer_default",
        batch_size: int = 200,
        batch_timeout_sec: float = 2.0,
        allow_extended_hours: bool = False,
        consumer_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.topic = topic
        self.table_name = table_name
        self.group_id = group_id
        self.batch_size = int(batch_size)
        self.batch_timeout_sec = float(batch_timeout_sec)
        self.allow_extended_hours = bool(allow_extended_hours)

        kafka_cfg = config.kafka_config
        kwargs = dict(
            bootstrap_servers=kafka_cfg["bootstrap_servers"],
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            auto_commit_interval_ms=5000,  # Commit every 5 seconds
            session_timeout_ms=90000,  # Align with lake/backfill consumers
            heartbeat_interval_ms=9000,
            request_timeout_ms=180000,
            max_poll_interval_ms=300000,  # 5 min max poll interval
            connections_max_idle_ms=540000,  # 9 min idle timeout
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        )
        if consumer_kwargs:
            kwargs.update(consumer_kwargs)

        self._consumer_kwargs = kwargs
        self.consumer = AIOKafkaConsumer(self.topic, **kwargs)
        self.db = None  # Will be initialized async in start()
        self._running = False
        self._consumer_started = False
        self._batch: List[List[Any]] = []
        self._dedup_keys: set = set()  # (symbol, ts_start)
        self._bg_tasks: List[asyncio.Task] = []
        self._restart_lock = asyncio.Lock()
        self._assignment_lost_since: Optional[float] = None

        # Retry configuration for Kafka errors
        self._max_kafka_retries = 5
        self._kafka_retry_base_delay = 1.0  # seconds
        self._assignment_watchdog_interval_sec = 5.0
        self._assignment_watchdog_threshold_sec = 30.0

        logger.info("ClickHouseBarWriter initialized", topic=self.topic, table=self.table_name, group=self.group_id)

    # ---------------- Lifecycle ----------------

    async def start(self) -> None:
        # Initialize async ClickHouse client using shared pool
        self.db = await ch_pool.get_async_client()
        await self.consumer.start()
        self._consumer_started = True
        self._running = True
        self._bg_tasks.append(asyncio.create_task(self._batch_flusher(), name=f"ch_writer_flush:{self.topic}"))
        self._bg_tasks.append(asyncio.create_task(self._consume_loop(), name=f"ch_writer_consume:{self.topic}"))
        self._bg_tasks.append(asyncio.create_task(self._assignment_watchdog(), name=f"ch_writer_watchdog:{self.topic}"))
        logger.info("ClickHouseBarWriter started", topic=self.topic)

    async def run(self) -> None:
        """Run the writer service (keeps it alive until stopped)."""
        await self.start()
        try:
            # Wait for all background tasks to complete
            await asyncio.gather(*self._bg_tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._running = False

        # Commit offsets before stopping to avoid UnknownMemberIdError
        commit_success = await self._safe_commit()
        if commit_success:
            logger.debug("Offsets committed", topic=self.topic)
        else:
            logger.warning("Offset commit failed", topic=self.topic)

        # Stop consumer to unblock the consume loop
        try:
            await asyncio.wait_for(self.consumer.stop(), timeout=10.0)
            self._consumer_started = False
        except asyncio.TimeoutError:
            logger.warning("Consumer stop timed out", topic=self.topic)
        except Exception as exc:
            logger.warning("Consumer stop failed", topic=self.topic, error=str(exc))

        # Flush any remaining batch
        try:
            if self._batch:
                await self._flush_batch()
        except Exception as exc:
            logger.warning("Flush on stop failed", error=str(exc))

        # Cancel background tasks
        for t in self._bg_tasks:
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        # Close ClickHouse connection
        if self.db:
            try:
                await self.db.close()
            except Exception as exc:
                logger.warning("DB close failed", topic=self.topic, error=str(exc))

        logger.info("ClickHouseBarWriter stopped", topic=self.topic)

    # ---------------- Kafka consume & batch flush ----------------

    async def _safe_commit(self) -> bool:
        """Commit offsets with retry logic for transient Kafka errors.

        Returns:
            True if commit succeeded, False if all retries failed
        """
        for attempt in range(1, self._max_kafka_retries + 1):
            try:
                await asyncio.wait_for(self.consumer.commit(), timeout=5.0)
                if attempt > 1:
                    logger.info("Kafka commit succeeded after retry", topic=self.topic, attempt=attempt)
                return True
            except asyncio.TimeoutError:
                delay = self._kafka_retry_base_delay * (2 ** (attempt - 1))
                logger.warning("Kafka commit timeout, retrying", topic=self.topic, attempt=attempt, retry_in_sec=delay)
                if attempt < self._max_kafka_retries:
                    await asyncio.sleep(delay)
            except (CommitFailedError, KafkaConnectionError) as exc:
                delay = self._kafka_retry_base_delay * (2 ** (attempt - 1))
                logger.warning("Kafka commit failed, retrying", topic=self.topic, attempt=attempt, error=str(exc), retry_in_sec=delay)
                if attempt < self._max_kafka_retries:
                    await asyncio.sleep(delay)
            except Exception as exc:
                # Non-retriable error
                if self._is_assignment_error(exc):
                    logger.warning("Kafka commit failed because consumer has no assignment", topic=self.topic, error=str(exc))
                    if self._running:
                        await self._restart_consumer(reason="commit_no_assignment")
                    return False
                logger.error("Kafka commit failed with non-retriable error", topic=self.topic, error=str(exc), exc_info=True)
                return False

        logger.error("Kafka commit failed after all retries", topic=self.topic, attempts=self._max_kafka_retries)
        return False

    async def _consume_loop(self) -> None:
        """Consume loop with retry logic for transient Kafka errors.

        This loop is hardened against:
        - Coordinator failures (UnknownError during auto-commit)
        - Connection errors
        - Broker restarts

        Strategy: On retriable errors, restart the consumer with exponential backoff.
        """
        attempt = 0
        while self._running:
            try:
                attempt += 1
                if attempt > 1:
                    logger.info("Restarting consumer", topic=self.topic, attempt=attempt)

                async for msg in self.consumer:
                    row = self._record_to_row(msg)
                    if row is None:
                        continue
                    key = (row[0], row[1])  # (symbol, ts_start)
                    if key in self._dedup_keys:
                        continue
                    self._dedup_keys.add(key)
                    self._batch.append(row)
                    if len(self._batch) >= self.batch_size:
                        await self._flush_batch()

                # Normal exit can happen when the watchdog intentionally stops the
                # consumer to force a group rejoin.
                if self._running:
                    continue
                break

            except asyncio.CancelledError:
                logger.info("Consume loop cancelled", topic=self.topic)
                break

            except (KafkaError, KafkaConnectionError, CommitFailedError) as exc:
                error_msg = str(exc)
                # Check if this is a retriable error
                is_retriable = self._is_retriable_consume_error(exc)

                if is_retriable and attempt <= self._max_kafka_retries:
                    delay = self._kafka_retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Kafka consume error (retriable), restarting consumer",
                        topic=self.topic,
                        attempt=attempt,
                        error=error_msg,
                        retry_in_sec=delay
                    )

                    await asyncio.sleep(delay)

                    try:
                        await self._restart_consumer(reason="consume_retry")
                        logger.info("Consumer restarted successfully", topic=self.topic)
                        # Reset attempt counter after successful restart
                        attempt = 0
                    except Exception as start_exc:
                        logger.error("Failed to restart consumer", topic=self.topic, error=str(start_exc), exc_info=True)
                        # Continue to next retry
                else:
                    # Non-retriable or exhausted retries
                    logger.error(
                        "Kafka consume error (non-retriable or exhausted retries)",
                        topic=self.topic,
                        attempt=attempt,
                        error=error_msg,
                        exc_info=True
                    )
                    raise

            except Exception as exc:
                # Unknown error - log and re-raise
                logger.error("Consume loop error (unknown)", topic=self.topic, error=str(exc), exc_info=True)
                raise

    async def _assignment_watchdog(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._assignment_watchdog_interval_sec)
                if not self._running or not self._consumer_started:
                    continue
                try:
                    assignment = self.consumer.assignment()
                except Exception as exc:
                    if self._is_assignment_error(exc):
                        assignment = set()
                    else:
                        logger.warning("Kafka assignment probe failed", topic=self.topic, error=str(exc))
                        continue
                if assignment:
                    self._assignment_lost_since = None
                    continue
                now = asyncio.get_running_loop().time()
                if self._assignment_lost_since is None:
                    self._assignment_lost_since = now
                    logger.warning("Kafka consumer has no assigned partitions", topic=self.topic)
                    continue
                lost_for = now - self._assignment_lost_since
                if lost_for >= self._assignment_watchdog_threshold_sec:
                    logger.error(
                        "Kafka consumer assignment lost beyond threshold; restarting",
                        topic=self.topic,
                        lost_for_sec=round(lost_for, 3),
                    )
                    await self._restart_consumer(reason="assignment_watchdog")
                    self._assignment_lost_since = None
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Kafka assignment watchdog error", topic=self.topic, error=str(exc), exc_info=True)

    async def _restart_consumer(self, *, reason: str) -> None:
        async with self._restart_lock:
            if not self._running:
                return
            try:
                await asyncio.wait_for(self.consumer.stop(), timeout=5.0)
            except Exception as exc:
                logger.warning("Error stopping consumer for restart", topic=self.topic, reason=reason, error=str(exc))
            self._consumer_started = False
            await asyncio.sleep(1.0)
            self.consumer = AIOKafkaConsumer(self.topic, **self._consumer_kwargs)
            await self.consumer.start()
            self._consumer_started = True
            self._assignment_lost_since = None
            logger.info("Kafka consumer restarted", topic=self.topic, reason=reason)

    @staticmethod
    def _is_assignment_error(exc: Exception) -> bool:
        error_type = type(exc).__name__
        error_msg = str(exc)
        return (
            "IllegalStateError" in error_type
            or "No partitions assigned" in error_msg
            or "no partitions assigned" in error_msg.lower()
        )

    @staticmethod
    def _is_retriable_consume_error(exc: Exception) -> bool:
        error_msg = str(exc).lower()
        return (
            "unknownerror" in error_msg
            or "coordinator" in error_msg
            or "heartbeat" in error_msg
            or "session expired" in error_msg
            or "rebalance" in error_msg
            or "connection" in error_msg
            or "commit failed" in error_msg
            or "not available" in error_msg
        )

    async def _batch_flusher(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.batch_timeout_sec)
                if self._batch:
                    await self._flush_batch()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Batch flusher error", topic=self.topic, error=str(exc))

    async def _flush_batch(self) -> None:
        batch = self._batch
        self._batch = []
        self._dedup_keys.clear()
        if not batch:
            return
        try:
            # Use async client's insert method directly
            await self.db.insert(self.table_name, batch, column_names=list(self.COLS))
            logger.info("Inserted", table=self.table_name, rows=len(batch))
        except Exception as exc:
            logger.error("Insert batch failed", table=self.table_name, rows=len(batch), error=str(exc))

    # ---------------- Record normalization ----------------

    def _record_to_row(self, msg: ConsumerRecord) -> Optional[List[Any]]:
        try:
            payload = msg.value
            meta = payload.get("metadata", {}) or {}
            data = payload.get("data", {}) or {}

            symbol = data.get("symbol")
            ts_start = _us_to_dt(data.get("ts_start_us"))
            ts_end = _us_to_dt(data.get("ts_end_us"))
            open_ = data.get("open")
            high = data.get("high")
            low = data.get("low")
            close = data.get("close")
            vwap = data.get("vwap")
            volume = data.get("volume")
            trades = data.get("trades", 0)
            bid_last = data.get("bid_last")
            ask_last = data.get("ask_last")
            mid_last = data.get("mid_last")
            is_backfill = int(data.get("is_backfill", 0))

            ts_meta_us = meta.get("ts_us") or meta.get("ts") or 0
            ver = int((ts_meta_us or 0) // 1000)  # us→ms

            if not symbol or ts_start is None or ts_end is None:
                logger.warning("Drop invalid message", reason="missing symbol/ts", topic=self.topic)
                return None

            duration_sec = (ts_end - ts_start).total_seconds()

            # Filter out after-hours bars for intraday resolutions only unless this writer
            # is explicitly configured to retain extended-hours bars (needed for extended-session MA).
            # Daily bars (duration around 24h) arrive stamped outside RTH and must be retained.
            if duration_sec < 6 * 3600 and not self.allow_extended_hours and not _is_rth(ts_start):
                logger.debug("Drop after-hours bar", symbol=symbol, ts_start=str(ts_start), topic=self.topic)
                return None

            return [
                symbol,
                ts_start,
                ts_end,
                float(open_) if open_ is not None else None,
                float(high) if high is not None else None,
                float(low) if low is not None else None,
                float(close) if close is not None else None,
                float(vwap) if vwap is not None else None,
                float(volume) if volume is not None else 0.0,
                int(trades) if trades is not None else 0,
                float(bid_last) if bid_last is not None else None,
                float(ask_last) if ask_last is not None else None,
                float(mid_last) if mid_last is not None else None,
                ver,
                is_backfill,
            ]
        except Exception as exc:
            logger.error("Failed to parse record", topic=self.topic, error=str(exc))
            return None
