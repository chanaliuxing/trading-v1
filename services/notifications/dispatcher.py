from __future__ import annotations

import asyncio
import os
from typing import Iterable, Optional

from libs.logger import get_logger

from .base import NotificationEvent, Notifier

logger = get_logger(__name__)


class NotificationDispatcher:
    """Non-blocking queue-based notification dispatcher."""

    def __init__(
        self,
        notifiers: Iterable[Notifier],
        *,
        queue_size: int = 1000,
        worker_count: int = 1,
    ):
        self._notifiers = list(notifiers)
        self._queue: asyncio.Queue[NotificationEvent] = asyncio.Queue(maxsize=max(1, int(queue_size)))
        self._worker_count = max(1, int(worker_count))
        self._workers: list[asyncio.Task] = []
        self._running = False

        self._stats = {
            "published": 0,
            "dropped": 0,
            "sent": 0,
            "failed": 0,
        }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for idx in range(self._worker_count):
            t = asyncio.create_task(self._worker_loop(), name=f"notification-worker-{idx}")
            self._workers.append(t)

    def publish(self, event: NotificationEvent) -> bool:
        """Queue an event without blocking main execution path."""
        if not self._running:
            self.start()

        try:
            self._queue.put_nowait(event)
            self._stats["published"] += 1
            return True
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            logger.warning(
                "notification_queue_full_drop",
                strategy_id=event.strategy_id,
                symbol=event.symbol,
                event_type=event.event_type,
            )
            return False

    async def send_now(self, event: NotificationEvent) -> bool:
        """Send one event immediately, bypassing the queue."""
        self._stats["published"] += 1
        any_success = False
        for notifier in self._notifiers:
            try:
                ok = await notifier.send(event)
                if ok:
                    any_success = True
                    self._stats["sent"] += 1
                else:
                    self._stats["failed"] += 1
            except Exception as exc:  # pragma: no cover
                self._stats["failed"] += 1
                logger.error(
                    "notification_send_now_failed",
                    strategy_id=event.strategy_id,
                    symbol=event.symbol,
                    event_type=event.event_type,
                    error=str(exc),
                )
        return any_success

    async def _worker_loop(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                for notifier in self._notifiers:
                    ok = await notifier.send(event)
                    if ok:
                        self._stats["sent"] += 1
                    else:
                        self._stats["failed"] += 1
            except Exception as exc:  # pragma: no cover
                self._stats["failed"] += 1
                logger.error(
                    "notification_dispatch_failed",
                    strategy_id=event.strategy_id,
                    symbol=event.symbol,
                    error=str(exc),
                )
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        workers = list(self._workers)
        self._workers.clear()
        for t in workers:
            t.cancel()
        for t in workers:
            try:
                await t
            except asyncio.CancelledError:
                pass

        for notifier in self._notifiers:
            try:
                await notifier.close()
            except Exception:
                pass

    def stats(self) -> dict:
        return dict(self._stats)


class NotificationManager:
    """Pluggable manager that filters and dispatches events to all sinks."""

    def __init__(
        self,
        dispatcher: NotificationDispatcher,
        *,
        strategy_allowlist: Optional[Iterable[str]] = None,
    ):
        self.dispatcher = dispatcher
        allowlist = set()
        if strategy_allowlist:
            allowlist = {str(x).strip() for x in strategy_allowlist if str(x).strip()}
        self.strategy_allowlist = allowlist
        self._runtime_alert_dedupe_window_sec = max(
            0.0,
            float(os.getenv("RUNTIME_ALERT_DEDUPE_WINDOW_SEC", "60") or 60.0),
        )
        self._runtime_alert_state: dict[str, dict[str, float | int]] = {}

    @staticmethod
    def _runtime_alert_key(event: NotificationEvent) -> str:
        md = event.metadata if isinstance(event.metadata, dict) else {}
        event_type = str(event.event_type or "")
        status = str(event.status or "")
        strategy_id = str(event.strategy_id or "")
        symbol = str(event.symbol or "")
        reason = str(event.reason or "")
        service = str(md.get("service") or "")
        error_type = str(md.get("error_type") or "")
        # IB connection recovery emits several INFO state transitions during
        # startup/reconnect (ib_connected/system_recovering/system_resumed).
        # Treat them as one runtime alert class so Telegram only gets one
        # recovery message per dedupe window while preserving ERROR/CRITICAL.
        if (
            event_type == "system_alert"
            and strategy_id == "SYSTEM"
            and symbol == "IB_CONNECTION"
            and status not in {"ERROR", "CRITICAL"}
        ):
            return "|".join([event_type, status, strategy_id, symbol, service])
        return "|".join([event_type, status, strategy_id, symbol, reason, service, error_type])

    def _coalesce_runtime_alert(self, event: NotificationEvent) -> bool:
        if self._runtime_alert_dedupe_window_sec <= 0:
            return True
        if event.event_type not in {"system_alert", "reconciliation_alert"}:
            return True
        key = self._runtime_alert_key(event)
        now_ts = float(event.ts.timestamp())
        state = self._runtime_alert_state.get(key)
        if state is None:
            self._runtime_alert_state[key] = {"last_ts": now_ts, "suppressed": 0}
            return True

        last_ts = float(state.get("last_ts", 0.0) or 0.0)
        suppressed = int(state.get("suppressed", 0) or 0)
        if (now_ts - last_ts) < self._runtime_alert_dedupe_window_sec:
            state["suppressed"] = suppressed + 1
            self._runtime_alert_state[key] = state
            return False

        # Window elapsed: emit one message and attach how many duplicates were suppressed.
        state["last_ts"] = now_ts
        state["suppressed"] = 0
        self._runtime_alert_state[key] = state
        if suppressed > 0:
            meta = dict(event.metadata or {})
            meta["suppressed_duplicates_in_window"] = suppressed
            meta["dedupe_window_sec"] = self._runtime_alert_dedupe_window_sec
            event.metadata = meta
        return True

    def _allow_event(self, event: NotificationEvent) -> bool:
        if not self._coalesce_runtime_alert(event):
            return False
        if str(event.event_type or "").startswith("system_"):
            return True
        if self.strategy_allowlist and event.strategy_id not in self.strategy_allowlist:
            return False
        return True

    def publish(self, event: NotificationEvent) -> bool:
        if not self._allow_event(event):
            return False
        return self.dispatcher.publish(event)

    async def send_now(self, event: NotificationEvent) -> bool:
        if not self._allow_event(event):
            return False
        return await self.dispatcher.send_now(event)

    async def shutdown(self) -> None:
        await self.dispatcher.stop()

    def stats(self) -> dict:
        return self.dispatcher.stats()
