# services/market/writer/lake_writer.py
"""ClickHouse Lake Writer for raw ticks and quotes.
- Consumes tick/quote messages from Kafka (aiokafka)
- Writes to ClickHouse lake tables (lake.ticks_trades, lake.quotes_l1)
- Idempotent via ReplacingMergeTree(ver)
- Supports both 'quotes' and 'trades' modes
"""
import asyncio
import json
import time
from typing import List, Dict, Any, Optional, Tuple, Literal
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer
from aiokafka.structs import ConsumerRecord
from aiokafka.errors import KafkaError, KafkaConnectionError, CommitFailedError

from libs.db import ch_pool  # Use shared global connection pool
from libs.logger import get_logger
from libs.config import config

logger = get_logger(__name__)

def _us_to_dt(us: Optional[int]) -> Optional[datetime]:
    if us is None:
        return None
    try:
        # Timestamps from market_data are already in local time (America/New_York)
        # Create naive datetime for ClickHouse (session timezone = America/New_York)
        return datetime.fromtimestamp(us / 1_000_000)
    except Exception:
        return None

class ClickHouseLakeWriter:
    """Generic Kafka→ClickHouse writer for raw tick and quote data."""

    # Preferred column order for lake.ticks_trades (writer will auto-intersect with real table schema)
    TRADES_COLS: Tuple[str, ...] = (
        "symbol",
        "ts",
        "price",
        "size",
        "exchange",
        "is_rth",
        "source",
        "ver",
    )

    # Preferred column order for lake.quotes_l1 (writer will auto-intersect with real table schema)
    QUOTES_COLS: Tuple[str, ...] = (
        "symbol",
        "ts",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
        "exchange",
        "is_rth",
        "source",
        "ver",
    )

    def __init__(
        self,
        *,
        topic: str,
        mode: Literal["trades", "quotes"],
        group_id: str = "lake_writer_default",
        batch_size: int = 500,
        batch_timeout_sec: float = 1.0,
        consumer_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.topic = topic
        self.mode = mode
        self.group_id = group_id
        self.batch_size = int(batch_size)
        self.batch_timeout_sec = float(batch_timeout_sec)

        if mode == "trades":
            self.table_name = "lake.ticks_trades"
            self.cols = self.TRADES_COLS
        elif mode == "quotes":
            self.table_name = "lake.quotes_l1"
            self.cols = self.QUOTES_COLS
        else:
            raise ValueError(f"Invalid mode: {mode}. Must be 'trades' or 'quotes'")

        kafka_cfg = config.kafka_config

        # Auto-commit configuration:
        # - enable_auto_commit=True (default): Kafka commits offsets automatically
        #   Pros: Simple, automatic, reduces code complexity
        #   Cons: May commit before data is flushed to ClickHouse (at-most-once semantics)
        #          Coordinator errors during auto-commit can crash consumer
        # - enable_auto_commit=False: Manual commit after successful flush
        #   Pros: Safer (at-least-once), better control over commit timing
        #   Cons: More complex, needs manual commit calls after each flush
        #
        # Current choice: Keep auto-commit for simplicity, but harden error handling
        # to survive coordinator failures. Data idempotency via ReplacingMergeTree
        # means duplicate writes are acceptable.
        kwargs = dict(
            bootstrap_servers=kafka_cfg["bootstrap_servers"],
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            auto_commit_interval_ms=5000,  # Commit every 5 seconds
            session_timeout_ms=90000,  # 90s session timeout (more tolerant to GC pauses)
            heartbeat_interval_ms=9000,  # Heartbeat every 9s, below session timeout/3
            request_timeout_ms=180000,  # 180s request timeout to survive transient stalls
            max_poll_interval_ms=300000,  # 5 min max poll interval
            connections_max_idle_ms=540000,  # 9 min idle timeout
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        )
        if consumer_kwargs:
            kwargs.update(consumer_kwargs)

        self._consumer_kwargs = kwargs  # Store for consumer recreation on errors
        self.consumer = AIOKafkaConsumer(self.topic, **kwargs)
        self.db = None  # Will be initialized async in start()
        self._sync_db = None  # Dedicated sync client for threaded inserts
        self._running = False
        self._queue: asyncio.Queue[Optional[List[Any]]] = asyncio.Queue(maxsize=self.batch_size * 2)
        self._batch: List[List[Any]] = []
        self._dedup_keys: set = set()  # (symbol, ts_us)
        self._bg_tasks: List[asyncio.Task] = []

        # Retry configuration for Kafka errors
        self._max_kafka_retries = 10
        self._kafka_retry_base_delay = 2.0  # seconds

        # Optional per-session ClickHouse settings to reduce memory pressure.
        # Can be overridden via config key: clickhouse.session_settings
        # Defaults aim to be conservative for inserts.
        default_session_settings: Dict[str, Any] = {
            "max_threads": 1,
            "max_insert_threads": 1,
            # Bound per-query memory; server may still enforce a lower (total) cap
            "max_memory_usage": 1_073_741_824,  # 1 GiB
            # Disable uncompressed cache to reduce memory usage in some workloads
            "use_uncompressed_cache": 0,
        }
        self._session_settings: Dict[str, Any] = (
            config.get("clickhouse.session_settings", {}) or {}
        )
        # Only keep known-safe keys; allow integers/strings
        for k, v in default_session_settings.items():
            self._session_settings.setdefault(k, v)

        logger.info("ClickHouseLakeWriter initialized", topic=self.topic, table=self.table_name, mode=self.mode, group=self.group_id)

    # ---------------- Lifecycle ----------------

    async def start(self) -> None:
        # Initialize async ClickHouse client using shared pool
        self.db = await ch_pool.get_async_client()
        # Apply conservative session settings to reduce memory footprint
        await self._apply_session_settings()
        # Ensure ClickHouse client socket timeout is forgiving during Kafka hiccups.
        try:
            self.db.client.client_opts.setdefault("send_receive_timeout", 120)
        except Exception:
            pass
        # Create sync client for threaded inserts and align session settings
        self._sync_db = ch_pool.get_client()
        self._apply_session_settings_sync()
        await asyncio.to_thread(self._detect_table_columns_sync)
        await self.consumer.start()
        self._running = True
        self._bg_tasks.append(asyncio.create_task(self._writer_loop(), name=f"lake_writer_flush:{self.topic}"))
        self._bg_tasks.append(asyncio.create_task(self._consume_loop(), name=f"lake_writer_consume:{self.topic}"))
        logger.info("ClickHouseLakeWriter started", topic=self.topic, mode=self.mode)

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

        # Gracefully stop the consumer loop by putting a sentinel value in the queue
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            logger.warning("Queue full, cannot add sentinel value on stop", topic=self.topic, mode=self.mode)


        # Commit offsets before stopping to avoid UnknownMemberIdError
        commit_success = await self._safe_commit()
        if commit_success:
            logger.debug("Offsets committed", topic=self.topic, mode=self.mode)
        else:
            logger.warning("Offset commit failed", topic=self.topic, mode=self.mode)

        # Stop consumer
        try:
            await asyncio.wait_for(self.consumer.stop(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Consumer stop timed out", topic=self.topic, mode=self.mode)
        except Exception as exc:
            logger.warning("Consumer stop failed", topic=self.topic, mode=self.mode, error=str(exc))

        # Wait for background tasks to complete gracefully (avoid cancelling mid-flush)
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        # Final flush on exit
        await self._drain_queue_and_flush()


        # Close ClickHouse connection
        if self.db:
            try:
                await self.db.close()
            except Exception as exc:
                logger.warning("DB close failed", topic=self.topic, mode=self.mode, error=str(exc))
        self._sync_db = None

        logger.info("ClickHouseLakeWriter stopped", topic=self.topic, mode=self.mode)

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
                    logger.info("Kafka commit succeeded after retry", topic=self.topic, mode=self.mode, attempt=attempt)
                return True
            except asyncio.TimeoutError:
                delay = self._kafka_retry_base_delay * (2 ** (attempt - 1))
                logger.warning("Kafka commit timeout, retrying", topic=self.topic, mode=self.mode, attempt=attempt, retry_in_sec=delay)
                if attempt < self._max_kafka_retries:
                    await asyncio.sleep(delay)
            except (CommitFailedError, KafkaConnectionError) as exc:
                delay = self._kafka_retry_base_delay * (2 ** (attempt - 1))
                logger.warning("Kafka commit failed, retrying", topic=self.topic, mode=self.mode, attempt=attempt, error=str(exc), retry_in_sec=delay)
                if attempt < self._max_kafka_retries:
                    await asyncio.sleep(delay)
            except Exception as exc:
                error_text = str(exc)
                error_type = type(exc).__name__
                # Graceful shutdown path: consumer may already lose assignment while Ctrl+C is stopping loops.
                # Treat this as benign to avoid noisy error traceback.
                benign_shutdown_commit_errors = (
                    "No partitions assigned" in error_text
                    or "IllegalStateError" in error_type
                )
                if (not self._running) and benign_shutdown_commit_errors:
                    logger.warning(
                        "Kafka commit skipped during shutdown",
                        topic=self.topic,
                        mode=self.mode,
                        error=error_text,
                        error_type=error_type,
                    )
                    return False
                # Non-retriable error
                logger.error(
                    "Kafka commit failed with non-retriable error",
                    topic=self.topic,
                    mode=self.mode,
                    error=error_text,
                    error_type=error_type,
                    exc_info=True,
                )
                return False

        logger.error("Kafka commit failed after all retries", topic=self.topic, mode=self.mode, attempts=self._max_kafka_retries)
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
                    logger.info("Restarting consumer", topic=self.topic, mode=self.mode, attempt=attempt)

                async for msg in self.consumer:
                    row = self._record_to_row(msg)
                    if row is None:
                        continue
                    await self._queue.put(row)

                # Normal exit (consumer stopped)
                break

            except asyncio.CancelledError:
                logger.info("Consume loop cancelled", topic=self.topic, mode=self.mode)
                break

            except (KafkaError, KafkaConnectionError, CommitFailedError) as exc:
                error_msg = str(exc)
                # Check if this is a retriable error
                is_retriable = (
                    "UnknownError" in error_msg or
                    "coordinator" in error_msg.lower() or
                    "connection" in error_msg.lower() or
                    "commit failed" in error_msg.lower() or
                    "not available" in error_msg.lower()
                )

                if is_retriable and attempt <= self._max_kafka_retries:
                    delay = self._kafka_retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Kafka consume error (retriable), restarting consumer",
                        topic=self.topic,
                        mode=self.mode,
                        attempt=attempt,
                        error=error_msg,
                        retry_in_sec=delay
                    )

                    # Stop consumer and recreate to avoid "start twice" assertion error
                    try:
                        await asyncio.wait_for(self.consumer.stop(), timeout=5.0)
                    except Exception as stop_exc:
                        logger.warning("Error stopping consumer", topic=self.topic, mode=self.mode, error=str(stop_exc))

                    await asyncio.sleep(delay)

                    try:
                        # Recreate consumer object to avoid "start twice" assertion in aiokafka
                        self.consumer = AIOKafkaConsumer(self.topic, **self._consumer_kwargs)
                        await self.consumer.start()
                        logger.info("Consumer recreated and restarted successfully", topic=self.topic, mode=self.mode)
                        # Reset attempt counter after successful restart
                        attempt = 0
                    except Exception as start_exc:
                        logger.error("Failed to recreate consumer", topic=self.topic, mode=self.mode, error=str(start_exc), exc_info=True)
                        # Continue to next retry
                else:
                    # Non-retriable or exhausted retries
                    logger.error(
                        "Kafka consume error (non-retriable or exhausted retries)",
                        topic=self.topic,
                        mode=self.mode,
                        attempt=attempt,
                        error=error_msg,
                        exc_info=True
                    )
                    raise

            except Exception as exc:
                # Unknown error - log and re-raise
                logger.error("Consume loop error (unknown)", topic=self.topic, mode=self.mode, error=str(exc), exc_info=True)
                raise

    async def _writer_loop(self) -> None:
        while self._running:
            try:
                row: Optional[List[Any]] = None
                # Wait for the first item with a timeout; flush on idle
                try:
                    row = await asyncio.wait_for(self._queue.get(), timeout=self.batch_timeout_sec)
                except asyncio.TimeoutError:
                    if self._batch:
                        await self._flush_batch()
                    continue  # Start new wait cycle

                if row is None:  # Sentinel value
                    self._running = False
                    break

                self._add_to_batch(row)

                # Drain queue to fill batch quickly
                while len(self._batch) < self.batch_size:
                    try:
                        row = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if row is None:
                        self._running = False
                        break
                    self._add_to_batch(row)

                if self._batch and (not self._running or len(self._batch) >= self.batch_size):
                    await self._flush_batch()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Batch writer loop error", topic=self.topic, mode=self.mode, error=str(exc))
        # Flush any remaining rows before exiting loop
        if self._batch:
            await self._flush_batch()

    async def _drain_queue_and_flush(self) -> None:
        """Drains the queue of any remaining items and flushes them to the DB."""
        while not self._queue.empty():
            try:
                row = self._queue.get_nowait()
                if row:
                    self._add_to_batch(row)
            except asyncio.QueueEmpty:
                break
        if self._batch:
            await self._flush_batch()

    def _add_to_batch(self, row: List[Any]):
        """Add a row to the current batch, handling duplicates."""
        key = (row[0], row[1])  # (symbol, ts)
        if key in self._dedup_keys:
            return
        self._dedup_keys.add(key)
        self._batch.append(row)

    async def _flush_batch(self) -> None:
        batch = self._batch
        self._batch = []
        self._dedup_keys.clear()
        if not batch:
            return
        try:
            # Run inserts in a thread to keep the event loop free for Kafka heartbeats
            await asyncio.to_thread(self._insert_with_adaptive_retry_sync, batch)
            if self.mode == "quotes":
                try:
                    vix_rows = sum(1 for r in batch if r and r[0] in ("VIX", "VIXY"))
                except Exception:
                    vix_rows = 0
                if vix_rows > 0:
                    logger.info("Inserted quotes", table=self.table_name, mode=self.mode, rows=len(batch), vix_rows=vix_rows)
                else:
                    logger.info("Inserted", table=self.table_name, mode=self.mode, rows=len(batch))
            else:
                logger.info("Inserted", table=self.table_name, mode=self.mode, rows=len(batch))
        except Exception as exc:
            logger.error("Insert batch failed", table=self.table_name, mode=self.mode, rows=len(batch), error=str(exc))

    # ---------------- Record normalization ----------------

    def _record_to_row(self, msg: ConsumerRecord) -> Optional[List[Any]]:
        try:
            payload = msg.value
            meta = payload.get("metadata", {}) or {}
            data = payload.get("data", {}) or {}

            symbol = data.get("symbol")
            ts_us = data.get("ts_us") or data.get("timestamp_us")
            ts = _us_to_dt(ts_us)

            ts_meta_us = meta.get("ts_us") or meta.get("ts") or 0
            ver = int((ts_meta_us or 0) // 1000)  # us→ms

            exchange = data.get("exchange", "SMART")
            is_rth = int(data.get("is_rth", 1))
            source = data.get("source", "IBKR")

            if not symbol or ts is None:
                logger.warning("Drop invalid message", reason="missing symbol/ts", topic=self.topic, mode=self.mode)
                return None

            if self.mode == "trades":
                price = data.get("price")
                if price is None:
                    logger.warning("Drop invalid trade", reason="missing price", topic=self.topic)
                    return None
                row_map = {
                    "symbol": symbol,
                    "ts": ts,
                    "price": float(price),
                    "size": int(data.get("size") or 0),
                    "exchange": exchange,
                    "is_rth": is_rth,
                    "source": source,
                    "ver": ver,
                }
                return [row_map[col] for col in self.cols]
            elif self.mode == "quotes":
                bid = data.get("bid")
                ask = data.get("ask")
                if bid is None or ask is None:
                    logger.warning("Drop invalid quote", reason="missing bid/ask", topic=self.topic)
                    return None
                row_map = {
                    "symbol": symbol,
                    "ts": ts,
                    "bid": float(bid),
                    "ask": float(ask),
                    "bid_size": int(data.get("bid_size") or 0),
                    "ask_size": int(data.get("ask_size") or 0),
                    "exchange": exchange,
                    "is_rth": is_rth,
                    "source": source,
                    "ver": ver,
                }
                return [row_map[col] for col in self.cols]
            else:
                return None
        except Exception as exc:
            logger.error("Failed to parse record", topic=self.topic, mode=self.mode, error=str(exc))
            return None

    # ---------------- Internal helpers ----------------

    async def _apply_session_settings(self) -> None:
        """Apply configured session settings to the ClickHouse connection.
        This helps lower per-query memory usage and thread fanout."""
        if not self.db:
            return
        for key, value in (self._session_settings or {}).items():
            try:
                # Quote strings; pass numbers/booleans directly
                if isinstance(value, str):
                    await self.db.command(f"SET {key} = '{value}'")
                else:
                    await self.db.command(f"SET {key} = {int(value)}")
                logger.debug("Applied CH session setting", key=key, value=value)
            except Exception as exc:
                logger.warning("Failed to apply CH session setting", key=key, value=str(value), error=str(exc))

    def _apply_session_settings_sync(self) -> None:
        """Apply settings to the synchronous client used in executor threads."""
        if not self._sync_db:
            return
        for key, value in (self._session_settings or {}).items():
            try:
                if isinstance(value, str):
                    self._sync_db.command(f"SET {key} = '{value}'")
                else:
                    self._sync_db.command(f"SET {key} = {int(value)}")
                logger.debug("Applied CH session setting (sync)", key=key, value=value)
            except Exception as exc:
                logger.warning("Failed to apply CH session setting (sync)", key=key, value=str(value), error=str(exc))

    def _detect_table_columns_sync(self) -> None:
        """Detect actual table columns and keep only compatible writer columns.

        This prevents hard failures when ClickHouse schema differs across environments.
        """
        if not self._sync_db:
            return
        try:
            rows = self._sync_db.query(f"DESCRIBE TABLE {self.table_name}")
            existing_cols = {str(r.get("name")) for r in rows if r.get("name")}
        except Exception as exc:
            logger.warning(
                "Failed to detect table schema, using configured columns",
                table=self.table_name,
                mode=self.mode,
                error=str(exc),
            )
            return

        mandatory = {"symbol", "ts", "price"} if self.mode == "trades" else {"symbol", "ts", "bid", "ask"}
        if not mandatory.issubset(existing_cols):
            logger.error(
                "Table schema missing mandatory columns",
                table=self.table_name,
                mode=self.mode,
                mandatory=sorted(mandatory),
                existing=sorted(existing_cols),
            )
            return

        compatible_cols = tuple(col for col in self.cols if col in existing_cols)
        if not compatible_cols:
            logger.error(
                "No compatible columns detected for writer",
                table=self.table_name,
                mode=self.mode,
                existing=sorted(existing_cols),
            )
            return

        self.cols = compatible_cols
        logger.info(
            "Lake writer columns resolved from table schema",
            table=self.table_name,
            mode=self.mode,
            columns=list(self.cols),
        )

    @staticmethod
    def _is_memory_error(exc: Exception) -> bool:
        msg = str(exc) if exc else ""
        low = msg.lower()
        return (
            "memory limit" in low
            or "memory_limit_exceeded" in low
            or "code: 241" in low
        )

    def _insert_once_sync(self, rows: List[List[Any]]) -> None:
        # Prefer session-level settings; insert settings kept empty for compatibility
        if not self._sync_db:
            raise RuntimeError("Sync ClickHouse client is not initialized")
        self._sync_db.insert(self.table_name, rows, column_names=list(self.cols))

    def _insert_with_adaptive_retry_sync(self, rows: List[List[Any]]) -> None:
        """Attempt insert; on memory-limit errors, split batch or backoff/retry.

        Strategy:
        - If memory error and rows > 1: split into halves and insert each.
        - If single-row memory error: exponential backoff + retry a few times.
        """
        try:
            self._insert_once_sync(rows)
            return
        except Exception as exc:
            if not self._is_memory_error(exc):
                raise
            # Memory pressure handling
            n = len(rows)
            if n > 1:
                mid = n // 2
                logger.warning(
                    "Memory pressure on insert; splitting batch",
                    table=self.table_name,
                    mode=self.mode,
                    rows=n,
                )
                # Recurse on halves
                self._insert_with_adaptive_retry_sync(rows[:mid])
                self._insert_with_adaptive_retry_sync(rows[mid:])
                return

            # Single-row case: backoff and retry
            max_attempts = 5
            base_delay = 0.4  # seconds
            attempt = 1
            last_exc = exc
            while attempt <= max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                try:
                    time.sleep(delay)
                    self._insert_once_sync(rows)
                    logger.info(
                        "Recovered insert after backoff",
                        table=self.table_name,
                        mode=self.mode,
                        rows=len(rows),
                        attempt=attempt,
                        delay_sec=round(delay, 3),
                    )
                    return
                except Exception as e2:
                    last_exc = e2
                    if not self._is_memory_error(e2):
                        raise
                    attempt += 1
            # Exhausted attempts
            raise last_exc
