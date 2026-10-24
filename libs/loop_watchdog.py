"""
Event Loop Watchdog with Stack Dump
Captures stack traces when event loop blocking is detected
"""
import asyncio
import faulthandler
import sys
import time
import inspect

from libs.logger import get_logger

logger = get_logger(__name__)


async def loop_watchdog(threshold_ms=500, dump_after=5, interval=0.5):
    """
    Monitor event loop lag and dump stack traces when blocking detected.

    Args:
        threshold_ms: Log warning if lag exceeds this threshold (default: 500ms)
        dump_after: Dump full stack after N consecutive violations (default: 5)
        interval: How often to check (in seconds)
    """
    loop = asyncio.get_running_loop()
    last = loop.time()
    bad = 0

    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        lag_ms = (now - last - interval) * 1000
        last = now

        if lag_ms > threshold_ms:
            bad += 1
            # Use debug level for minor lag, warning for severe lag
            if lag_ms > 2000:
                logger.warning("Loop watchdog detected severe lag", lag_ms=int(lag_ms), consecutive=bad)
            else:
                logger.debug("Loop watchdog detected lag", lag_ms=int(lag_ms), consecutive=bad)

            if bad >= dump_after:
                logger.error("Loop watchdog triggering stack dump", lag_ms=int(lag_ms), consecutive=bad)

                # Only dump async task snapshot (skip thread dump as it's too noisy)
                # Full thread dump with faulthandler can be enabled for debugging if needed
                await task_snapshot()

                bad = 0  # Reset counter after dump
        else:
            bad = 0


async def task_snapshot():
    """Dump all running async tasks and their stacks"""
    tasks = asyncio.all_tasks()
    logger.info("[TASK SNAPSHOT] tasks running", task_count=len(tasks))

    for i, t in enumerate(tasks):
        if t is asyncio.current_task():
            continue

        try:
            name = t.get_name() or (t.get_coro().__qualname__ if hasattr(t.get_coro(), '__qualname__') else 'unknown')

            # Get task stack
            stack = t.get_stack()
            if stack:
                import traceback
                st = "".join(traceback.format_stack(stack[0]))[-500:]  # Last 500 chars for brevity
                logger.debug("task_snapshot", task_id=i, task_name=name, stack=st)
            else:
                logger.debug("task_snapshot", task_id=i, task_name=name, stack="<no stack>")
        except Exception as e:
            logger.debug("task_snapshot_error", task_id=i, error=str(e))
