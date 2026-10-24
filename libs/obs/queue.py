# libs/obs/queue.py
"""
Non-blocking observability queue with batch flushing.
Prevents monitoring from blocking the event loop.
"""
import asyncio
from enum import Enum
from typing import Dict, Any, List

from libs.logger import get_logger

logger = get_logger(__name__)


class Channel(str, Enum):
    """Data channels for different destinations."""
    LOGS = "logs"          # Loki
    CLICKHOUSE = "clickhouse"  # ClickHouse


class ObservabilityQueue:
    """
    Singleton queue for non-blocking observability data collection.

    Usage:
        from libs.obs.queue import obs_queue, Channel

        # Put data (non-blocking)
        await obs_queue.put(Channel.CLICKHOUSE, {
            "_table": "positions_snapshots",
            "data": {...}
        })

        # Start flushers at app startup
        obs_queue.start_flushers(loki_writer, ch_writer)

        # Stop flushers at app shutdown
        await obs_queue.stop_flushers()
    """

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ObservabilityQueue, cls).__new__(cls)
        return cls._instance

    def __init__(self, queue_size: int = 10000):
        if self._initialized:
            return

        self.queues: Dict[Channel, asyncio.Queue] = {
            Channel.LOGS: asyncio.Queue(maxsize=queue_size),
            Channel.CLICKHOUSE: asyncio.Queue(maxsize=queue_size)
        }
        self.flush_tasks: List[asyncio.Task] = []
        self._initialized = True
        logger.info("ObservabilityQueue initialized", extra={"queue_size": queue_size})

    async def put(self, channel: Channel, item: Dict[str, Any]):
        """Non-blocking put to specified channel queue."""
        try:
            self.queues[channel].put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(
                "Observability queue full - dropping item",
                extra={
                    "event": "queue_full",
                    "channel": channel.value
                }
            )

    def start_flushers(self, loki_writer, ch_writer):
        """Start background flusher tasks."""
        self.flush_tasks.append(
            asyncio.create_task(self._log_flusher(loki_writer))
        )
        self.flush_tasks.append(
            asyncio.create_task(self._clickhouse_flusher(ch_writer))
        )
        logger.info(f"{len(self.flush_tasks)} queue flushers started")

    async def stop_flushers(self):
        """Gracefully stop all flusher tasks."""
        logger.info("Stopping queue flushers...")

        # Wait for queues to drain
        for q in self.queues.values():
            await q.join()

        # Cancel flusher tasks
        for task in self.flush_tasks:
            task.cancel()

        # Wait for cancellation
        await asyncio.gather(*self.flush_tasks, return_exceptions=True)
        logger.info("Queue flushers stopped")

    async def _log_flusher(self, loki_writer, batch_size: int = 500, flush_interval: int = 2):
        """Background task: batch flush logs to Loki."""
        while True:
            try:
                batch = await self._gather_batch(
                    self.queues[Channel.LOGS],
                    batch_size,
                    flush_interval
                )
                if batch:
                    try:
                        await loki_writer.write_batch(batch)
                    except Exception as e:
                        logger.error(f"Failed to flush logs to Loki: {e}", exc_info=True)
            except asyncio.CancelledError:
                logger.info("Log flusher cancelled")
                break
            except Exception as e:
                logger.error(f"Error in log flusher: {e}", exc_info=True)
                await asyncio.sleep(flush_interval)

    async def _clickhouse_flusher(self, ch_writer, batch_size: int = 500, flush_interval: int = 2):
        """Background task: batch flush data to ClickHouse."""
        while True:
            try:
                items = await self._gather_batch(
                    self.queues[Channel.CLICKHOUSE],
                    batch_size,
                    flush_interval
                )
                if not items:
                    continue

                # Group by table name
                batches_by_table: Dict[str, List[Dict]] = {}
                for item in items:
                    table = item.get("_table")
                    if not table:
                        logger.warning(f"Item missing '_table' field: {item}")
                        continue

                    if table not in batches_by_table:
                        batches_by_table[table] = []
                    batches_by_table[table].append(item["data"])

                # Write each table's batch
                for table, batch in batches_by_table.items():
                    try:
                        await ch_writer.write_batch(table, batch)
                    except Exception as e:
                        logger.error(f"Failed to flush to ClickHouse table '{table}': {e}", exc_info=True)

            except asyncio.CancelledError:
                logger.info("ClickHouse flusher cancelled")
                break
            except Exception as e:
                logger.error(f"Error in ClickHouse flusher: {e}", exc_info=True)
                await asyncio.sleep(flush_interval)

    async def _gather_batch(self, queue: asyncio.Queue, batch_size: int, flush_interval: int) -> List[Dict]:
        """Gather batch from queue up to batch_size or timeout."""
        batch = []
        try:
            # Wait for first item (with timeout)
            first_item = await asyncio.wait_for(queue.get(), timeout=flush_interval)
            batch.append(first_item)
            queue.task_done()

            # Collect remaining items (non-blocking)
            while len(batch) < batch_size:
                try:
                    item = queue.get_nowait()
                    batch.append(item)
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break  # No more items, flush what we have

        except asyncio.TimeoutError:
            pass  # Timeout, return empty or partial batch

        return batch


# Global singleton instance
obs_queue = ObservabilityQueue()
