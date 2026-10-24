import asyncio
import sys
from pathlib import Path
from datetime import timedelta

import pytest

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.notifications import NotificationDispatcher, NotificationEvent, Notifier
from services.notifications.dispatcher import NotificationManager


@pytest.fixture
def anyio_backend():
    return "asyncio"


class BlockingNotifier(Notifier):
    def __init__(self):
        self.calls = 0
        self._gate = asyncio.Event()

    async def send(self, event: NotificationEvent) -> bool:
        self.calls += 1
        await self._gate.wait()
        return True

    def release(self):
        self._gate.set()


@pytest.mark.anyio
async def test_dispatcher_is_non_blocking_and_drops_when_queue_full():
    notifier = BlockingNotifier()
    dispatcher = NotificationDispatcher([notifier], queue_size=1, worker_count=1)

    e1 = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=10,
        order_type="MKT",
        decision_id="d1",
    )
    e2 = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=11,
        order_type="MKT",
        decision_id="d2",
    )
    e3 = NotificationEvent(
        event_type="order_submitted",
        status="SUBMITTED",
        strategy_id="qqq_intraday_001",
        symbol="TQQQ",
        side="BUY",
        qty=12,
        order_type="MKT",
        decision_id="d3",
    )

    assert dispatcher.publish(e1) is True
    await asyncio.sleep(0.01)
    assert dispatcher.publish(e2) is True

    start = asyncio.get_event_loop().time()
    assert dispatcher.publish(e3) is False
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.01

    notifier.release()
    await asyncio.sleep(0.05)

    stats = dispatcher.stats()
    assert stats["published"] == 2
    assert stats["dropped"] == 1

    await dispatcher.stop()


@pytest.mark.anyio
async def test_notification_manager_send_now_bypasses_allowlist_for_system_alert():
    notifier = BlockingNotifier()
    notifier.release()
    dispatcher = NotificationDispatcher([notifier], queue_size=1, worker_count=1)
    manager = NotificationManager(dispatcher=dispatcher, strategy_allowlist=["qqq_intraday_001"])

    event = NotificationEvent(
        event_type="system_alert",
        status="CRITICAL",
        strategy_id="SYSTEM",
        symbol="market_data",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="service_crashed",
    )

    assert await manager.send_now(event) is True
    assert notifier.calls == 1

    await dispatcher.stop()


@pytest.mark.anyio
async def test_notification_manager_runtime_alert_dedupes_within_window(monkeypatch):
    monkeypatch.setenv("RUNTIME_ALERT_DEDUPE_WINDOW_SEC", "60")
    notifier = BlockingNotifier()
    notifier.release()
    dispatcher = NotificationDispatcher([notifier], queue_size=10, worker_count=1)
    manager = NotificationManager(dispatcher=dispatcher, strategy_allowlist=["qqq_intraday_001", "system_reconciler"])

    from datetime import timedelta
    base_ts = NotificationEvent(
        event_type="reconciliation_alert",
        status="WARN",
        strategy_id="system_reconciler",
        symbol="SYSTEM",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
    ).ts

    e1 = NotificationEvent(
        event_type="reconciliation_alert",
        status="WARN",
        strategy_id="system_reconciler",
        symbol="SYSTEM",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="orphans_detected",
        ts=base_ts,
    )
    e2 = NotificationEvent(
        event_type="reconciliation_alert",
        status="WARN",
        strategy_id="system_reconciler",
        symbol="SYSTEM",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="orphans_detected",
        ts=base_ts + timedelta(seconds=10),
    )
    e3 = NotificationEvent(
        event_type="reconciliation_alert",
        status="WARN",
        strategy_id="system_reconciler",
        symbol="SYSTEM",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="orphans_detected",
        ts=base_ts + timedelta(seconds=20),
    )

    assert manager.publish(e1) is True
    await asyncio.sleep(0.02)
    assert manager.publish(e2) is False
    assert manager.publish(e3) is False
    await asyncio.sleep(0.05)
    assert notifier.calls == 1
    await dispatcher.stop()


@pytest.mark.anyio
async def test_notification_manager_runtime_alert_emits_suppressed_count_after_window(monkeypatch):
    monkeypatch.setenv("RUNTIME_ALERT_DEDUPE_WINDOW_SEC", "30")
    notifier = BlockingNotifier()
    notifier.release()
    dispatcher = NotificationDispatcher([notifier], queue_size=10, worker_count=1)
    manager = NotificationManager(dispatcher=dispatcher, strategy_allowlist=["qqq_intraday_001"])

    base = NotificationEvent(
        event_type="system_alert",
        status="CRITICAL",
        strategy_id="SYSTEM",
        symbol="market_data",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="service_crashed",
    )
    dup = NotificationEvent(**{**base.__dict__, "ts": base.ts + timedelta(seconds=5)})
    after = NotificationEvent(**{**base.__dict__, "ts": base.ts + timedelta(seconds=35)})

    assert manager.publish(base) is True
    assert manager.publish(dup) is False
    assert manager.publish(after) is True
    assert int(after.metadata.get("suppressed_duplicates_in_window", 0)) == 1
    await asyncio.sleep(0.05)
    assert notifier.calls == 2
    await dispatcher.stop()


@pytest.mark.anyio
async def test_notification_manager_coalesces_ib_connection_info_alerts_across_reasons(monkeypatch):
    monkeypatch.setenv("RUNTIME_ALERT_DEDUPE_WINDOW_SEC", "60")
    notifier = BlockingNotifier()
    notifier.release()
    dispatcher = NotificationDispatcher([notifier], queue_size=10, worker_count=1)
    manager = NotificationManager(dispatcher=dispatcher, strategy_allowlist=["qqq_intraday_001"])

    base_ts = NotificationEvent(
        event_type="system_alert",
        status="INFO",
        strategy_id="SYSTEM",
        symbol="IB_CONNECTION",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="ib_connected",
        metadata={"service": "IB_CONNECTION", "error_type": "ib_connected"},
    ).ts

    e1 = NotificationEvent(
        event_type="system_alert",
        status="INFO",
        strategy_id="SYSTEM",
        symbol="IB_CONNECTION",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="ib_connected",
        metadata={"service": "IB_CONNECTION", "error_type": "ib_connected"},
        ts=base_ts,
    )
    e2 = NotificationEvent(
        event_type="system_alert",
        status="INFO",
        strategy_id="SYSTEM",
        symbol="IB_CONNECTION",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="system_recovering",
        metadata={"service": "IB_CONNECTION", "error_type": "system_recovering"},
        ts=base_ts + timedelta(seconds=5),
    )
    e3 = NotificationEvent(
        event_type="system_alert",
        status="INFO",
        strategy_id="SYSTEM",
        symbol="IB_CONNECTION",
        side="N/A",
        qty=0.0,
        order_type="SYSTEM",
        decision_id="",
        reason="system_resumed",
        metadata={"service": "IB_CONNECTION", "error_type": "system_resumed"},
        ts=base_ts + timedelta(seconds=10),
    )

    assert manager.publish(e1) is True
    assert manager.publish(e2) is False
    assert manager.publish(e3) is False
    await asyncio.sleep(0.05)
    assert notifier.calls == 1
    await dispatcher.stop()
