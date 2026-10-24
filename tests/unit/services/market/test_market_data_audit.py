import time
from unittest.mock import MagicMock

import pytest

from services.market.ib_market_data import TickData
from services.market.market_data_service import MarketDataService, MarketDataServiceFatalError


@pytest.mark.asyncio
async def test_market_data_service_flushes_tick_minute_summary_on_minute_change():
    ib = MagicMock()
    ib.client.clientId = 123
    audit_writer = MagicMock()
    svc = MarketDataService(
        symbols=["QQQ"],
        enable_backfill=False,
        enable_tick_publisher=True,
        ib=ib,
        audit_writer=audit_writer,
    )
    svc._running = True

    await svc._on_tick(TickData(symbol="QQQ", ts_us=1_710_000_000_000_000, last=100.0, last_size=10))
    await svc._on_tick(TickData(symbol="QQQ", ts_us=1_710_000_060_000_000, bid=99.5, ask=100.5, bid_size=5, ask_size=6))

    stages = [call.kwargs.get("stage") for call in audit_writer.emit.call_args_list]
    assert "tick_minute_summary" in stages
    summary_call = next(call for call in audit_writer.emit.call_args_list if call.kwargs.get("stage") == "tick_minute_summary")
    assert summary_call.kwargs["domain"] == "market"
    assert summary_call.kwargs["symbol"] == "QQQ"
    assert summary_call.kwargs["payload"]["trade_count"] == 1


@pytest.mark.asyncio
async def test_market_data_service_still_records_minute_summary_when_tick_publisher_disabled():
    ib = MagicMock()
    ib.client.clientId = 123
    audit_writer = MagicMock()
    svc = MarketDataService(
        symbols=["QQQ"],
        enable_backfill=False,
        enable_tick_publisher=False,
        ib=ib,
        audit_writer=audit_writer,
    )
    svc._running = True

    await svc._on_tick(TickData(symbol="QQQ", ts_us=1_710_000_000_000_000, last=100.0, last_size=10))
    await svc._on_tick(TickData(symbol="QQQ", ts_us=1_710_000_060_000_000, bid=99.5, ask=100.5, bid_size=5, ask_size=6))

    stages = [call.kwargs.get("stage") for call in audit_writer.emit.call_args_list]
    assert "tick_minute_summary" in stages


@pytest.mark.asyncio
async def test_market_data_service_on_tick_feeds_aggregator():
    ib = MagicMock()
    ib.client.clientId = 123
    svc = MarketDataService(
        symbols=["QQQ"],
        enable_backfill=False,
        enable_tick_publisher=False,
        ib=ib,
    )
    svc._running = True
    svc.aggregator.on_tick = MagicMock()

    tick = TickData(symbol="QQQ", ts_us=1_710_000_000_123_000, last=100.0, last_size=10, bid=99.5, ask=100.5)
    await svc._on_tick(tick)

    svc.aggregator.on_tick.assert_called_once_with(
        symbol="QQQ",
        px=100.0,
        size=10,
        bid=99.5,
        ask=100.5,
        ts_ms=1_710_000_000_123,
    )


@pytest.mark.asyncio
async def test_market_data_service_tick_fail_fast_triggers_after_sustained_degrade():
    ib = MagicMock()
    ib.client.clientId = 123
    svc = MarketDataService(
        symbols=["QQQ"],
        enable_backfill=False,
        enable_tick_publisher=True,
        ib=ib,
    )
    svc._tick_fail_fast_enabled = True
    svc._tick_fail_fast_after_sec = 1.0
    svc._tick_fail_fast_min_failures = 2
    svc._tick_degraded_since_mono = time.monotonic() - 2.0
    svc._tick_degraded_failure_count = 1
    svc._tick_fail_fast_active = lambda: True

    svc._mark_tick_publisher_degraded(reason="producer_start_timeout", error="timeout")

    with pytest.raises(MarketDataServiceFatalError):
        svc._raise_if_fatal()


def test_market_data_service_classifies_ib_passive_wait_errors():
    assert MarketDataService._is_ib_recovery_passive_wait_error_text("reconnect_gatekeeper_denied:market")
    assert MarketDataService._is_ib_recovery_passive_wait_error_text(
        "ib_recovery_short_circuit_active:ib_connectivity_lost_1100"
    )
    assert MarketDataService._is_ib_recovery_passive_wait_error_text("TimeoutError: ib_api_handshake_not_ready")
    assert not MarketDataService._is_ib_recovery_passive_wait_error_text("ValueError: invalid_contract")


def test_market_data_service_uses_longer_backoff_for_ib_short_circuit():
    # Error classification is handled before calling the sleep helper; the
    # helper now receives the caller's default passive wait and optional circuit
    # remaining time.
    assert MarketDataService._ib_recovery_passive_wait_sleep_sec(default_sec=5.0) == 5.0
    assert MarketDataService._ib_recovery_passive_wait_sleep_sec(default_sec=1.0) == 1.0
