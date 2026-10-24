import asyncio
from unittest.mock import AsyncMock

import pytest

from services.market.writer.ch_writer import ClickHouseBarWriter


@pytest.mark.asyncio
async def test_safe_commit_restarts_consumer_when_assignment_is_lost():
    class _Consumer:
        async def commit(self):
            raise RuntimeError("IllegalStateError: No partitions assigned")

    writer = ClickHouseBarWriter.__new__(ClickHouseBarWriter)
    writer.topic = "md.bar.5s.v2"
    writer.consumer = _Consumer()
    writer._max_kafka_retries = 1
    writer._kafka_retry_base_delay = 0.0
    writer._running = True
    writer._restart_consumer = AsyncMock()

    ok = await writer._safe_commit()

    assert ok is False
    writer._restart_consumer.assert_awaited_once_with(reason="commit_no_assignment")


def test_is_assignment_error_detects_aiokafka_no_partition_state():
    assert ClickHouseBarWriter._is_assignment_error(RuntimeError("No partitions assigned")) is True
    assert ClickHouseBarWriter._is_assignment_error(RuntimeError("temporary network hiccup")) is False


@pytest.mark.asyncio
async def test_assignment_watchdog_restarts_consumer_after_threshold():
    class _Consumer:
        def assignment(self):
            return set()

    writer = ClickHouseBarWriter.__new__(ClickHouseBarWriter)
    writer.topic = "md.bar.5s.v2"
    writer.consumer = _Consumer()
    writer._running = True
    writer._consumer_started = True
    writer._assignment_lost_since = asyncio.get_running_loop().time() - 31.0
    writer._assignment_watchdog_interval_sec = 0.0
    writer._assignment_watchdog_threshold_sec = 30.0
    writer._restart_consumer = AsyncMock(side_effect=_stop_writer(writer))

    await writer._assignment_watchdog()

    writer._restart_consumer.assert_awaited_once_with(reason="assignment_watchdog")
    assert writer._assignment_lost_since is None


@pytest.mark.asyncio
async def test_assignment_watchdog_clears_loss_state_when_assignment_returns():
    class _Consumer:
        def __init__(self):
            self.calls = 0

        def assignment(self):
            self.calls += 1
            return {"partition-0"}

    writer = ClickHouseBarWriter.__new__(ClickHouseBarWriter)
    writer.topic = "md.bar.5s.v2"
    writer.consumer = _Consumer()
    writer._running = True
    writer._consumer_started = True
    writer._assignment_lost_since = 123.0
    writer._assignment_watchdog_interval_sec = 0.0
    writer._assignment_watchdog_threshold_sec = 30.0
    writer._restart_consumer = AsyncMock()

    sleep_calls = 0

    async def _sleep(_):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            writer._running = False

    original_sleep = asyncio.sleep
    try:
        asyncio.sleep = _sleep
        await writer._assignment_watchdog()
    finally:
        asyncio.sleep = original_sleep

    writer._restart_consumer.assert_not_awaited()
    assert writer._assignment_lost_since is None


def test_is_retriable_consume_error_matches_heartbeat_and_coordinator_failures():
    assert ClickHouseBarWriter._is_retriable_consume_error(RuntimeError("Heartbeat session expired")) is True
    assert ClickHouseBarWriter._is_retriable_consume_error(RuntimeError("group coordinator unavailable")) is True
    assert ClickHouseBarWriter._is_retriable_consume_error(RuntimeError("validation error")) is False


def _stop_writer(writer: ClickHouseBarWriter):
    async def _inner(**_kwargs):
        writer._running = False
        writer._assignment_lost_since = None

    return _inner
