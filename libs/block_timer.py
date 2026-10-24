"""
BlockTimer - Detect blocking operations on the event loop
"""
import time
import threading
from libs.logger import get_logger

logger = get_logger("perf")


class BlockTimer:
    """
    Context manager to detect blocking operations.

    Usage:
        with BlockTimer("clickhouse.insert", warn_ms=30):
            client.insert(table, rows)

    Logs warning if operation takes longer than warn_ms and runs on MainThread.
    """

    def __init__(self, name: str, warn_ms: int = 50):
        self.name = name
        self.warn_ms = warn_ms
        self.t0 = None
        self.thread = None

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.thread = threading.current_thread().name
        return self

    def __exit__(self, *exc):
        dt_ms = (time.perf_counter() - self.t0) * 1000

        if dt_ms > self.warn_ms:
            logger.warning(
                "BLOCKING OPERATION DETECTED",
                where=self.name,
                duration_ms=int(dt_ms),
                thread=self.thread,
                is_main_thread=self.thread == "MainThread"
            )
