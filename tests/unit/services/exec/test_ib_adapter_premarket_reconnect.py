import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.exec.adapters.ib_adapter import IBInsyncAdapter
from services.exec.adapters import ib_adapter as ib_adapter_mod


class _FakeExecIBProbeOK:
    def __init__(self):
        self.connected = True

    def isConnected(self):
        return self.connected

    async def reqCurrentTimeAsync(self):
        return "ok"


def test_premarket_hard_failure_backoff_escalates_and_caps(monkeypatch):
    adapter = IBInsyncAdapter()
    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "PREMARKET")

    assert adapter._next_reconnect_sleep(hard_failure=True) == 5.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 10.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 20.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 30.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 30.0


def test_non_premarket_or_non_hard_failure_stays_fast(monkeypatch):
    adapter = IBInsyncAdapter()

    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "RTH")
    assert adapter._next_reconnect_sleep(hard_failure=True) == 2.0

    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "PREMARKET")
    assert adapter._next_reconnect_sleep(hard_failure=False) == 2.0


def test_afterhours_hard_failure_backoff_escalates_and_caps(monkeypatch):
    # AFTERHOURS covers the 19:45 ET Gateway daily restart window. Without
    # exponential backoff the reconnector would hammer TCP every ~7s for 20+
    # minutes and trigger IBKR pacing protection.
    adapter = IBInsyncAdapter()
    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "AFTERHOURS")

    assert adapter._next_reconnect_sleep(hard_failure=True) == 5.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 10.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 20.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 40.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 80.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 120.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 120.0


def test_offhours_hard_failure_backoff_escalates_and_caps(monkeypatch):
    adapter = IBInsyncAdapter()
    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "OFFHOURS")

    assert adapter._next_reconnect_sleep(hard_failure=True) == 5.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 10.0
    assert adapter._next_reconnect_sleep(hard_failure=True) == 20.0
    # Cap at IB_OFFHOURS_RECONNECT_MAX_SEC default 120.
    for _ in range(6):
        delay = adapter._next_reconnect_sleep(hard_failure=True)
    assert delay == 120.0


def test_afterhours_soft_failure_stays_fast(monkeypatch):
    # Soft failures (non-connectivity errors) should not trigger backoff even
    # off-hours — those are transient and should recover quickly.
    adapter = IBInsyncAdapter()
    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "AFTERHOURS")
    assert adapter._next_reconnect_sleep(hard_failure=False) == 2.0

    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "OFFHOURS")
    assert adapter._next_reconnect_sleep(hard_failure=False) == 2.0


def test_reconnect_backoff_reset():
    adapter = IBInsyncAdapter()
    adapter._next_reconnect_sleep(hard_failure=True)
    adapter._reset_reconnect_backoff()
    assert adapter._reconnect_failures == 0
    assert adapter._reconnect_sleep_sec == 2.0


def test_probe_throttle_disabled_in_rth(monkeypatch):
    adapter = IBInsyncAdapter()
    adapter._fresh_probe_min_interval_sec = 10.0
    adapter._fresh_probe_min_interval_rth_sec = 1.0
    adapter._owner_probe_min_interval_sec = 10.0
    adapter._owner_probe_min_interval_rth_sec = 2.0
    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "RTH")

    assert adapter._current_fresh_probe_min_interval_sec() == 1.0
    assert adapter._current_owner_probe_min_interval_sec() == 2.0


def test_probe_throttle_kept_offhours(monkeypatch):
    adapter = IBInsyncAdapter()
    adapter._fresh_probe_min_interval_sec = 10.0
    adapter._fresh_probe_min_interval_rth_sec = 0.0
    adapter._owner_probe_min_interval_sec = 10.0
    adapter._owner_probe_min_interval_rth_sec = 0.0
    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "OFFHOURS")

    assert adapter._current_fresh_probe_min_interval_sec() == 10.0
    assert adapter._current_owner_probe_min_interval_sec() == 10.0


@pytest.mark.asyncio
async def test_start_degraded_when_initial_connect_fails(monkeypatch):
    adapter = IBInsyncAdapter()
    created = []
    bound = {"called": False}

    async def _fail_connect():
        raise TimeoutError("handshake timeout")

    def _bind():
        bound["called"] = True

    monkeypatch.setattr(adapter, "_connect", _fail_connect)
    monkeypatch.setattr(adapter, "_bind_events", _bind)
    monkeypatch.setattr(asyncio, "create_task", lambda coro, **kwargs: created.append(coro))

    await adapter.start()

    assert adapter._running is True
    assert bound["called"] is True
    assert len(created) == 2
    assert {coro.cr_code.co_name for coro in created} == {"_reconnector", "_probe_loop"}
    for coro in created:
        coro.close()


@pytest.mark.asyncio
async def test_start_managed_mode_does_not_spawn_reconnector(monkeypatch):
    adapter = IBInsyncAdapter()
    adapter.configure_managed_mode(enabled=True)
    created = []

    async def _connect(*, initiated_by_manager=False):
        assert initiated_by_manager is True
        return None

    monkeypatch.setattr(adapter, "_connect", _connect)
    monkeypatch.setattr(asyncio, "create_task", lambda coro, **kwargs: created.append(coro))

    await adapter.start()

    assert len(created) == 1
    assert created[0].cr_code.co_name == "_probe_loop"
    created[0].close()


@pytest.mark.asyncio
async def test_start_reports_initial_connect_failure_to_gatekeeper(monkeypatch):
    adapter = IBInsyncAdapter()
    created = []
    gatekeeper = MagicMock()
    gatekeeper.acquire = AsyncMock(return_value=True)

    class _FailingIB:
        def isConnected(self):
            return False

        async def connectAsync(self, *args, **kwargs):
            raise TimeoutError("handshake timeout")

    adapter.ib = _FailingIB()
    monkeypatch.setattr(adapter, "_bind_events", lambda: None)
    monkeypatch.setattr(asyncio, "create_task", lambda coro, **kwargs: created.append(coro))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    adapter._reconnect_gatekeeper = gatekeeper

    await adapter.start()

    gatekeeper.report_failure.assert_called_once()
    assert gatekeeper.report_failure.call_args.kwargs["caller"] == "exec_adapter_connect"
    for coro in created:
        coro.close()


@pytest.mark.asyncio
async def test_connect_keeps_client_id_after_timeout(monkeypatch):
    adapter = IBInsyncAdapter(client_id=207)
    gatekeeper = MagicMock()
    gatekeeper.acquire = AsyncMock(return_value=True)
    gatekeeper.report_failure = MagicMock()
    adapter._reconnect_gatekeeper = gatekeeper

    class _FailingIB:
        def isConnected(self):
            return False

        async def connectAsync(self, *args, **kwargs):
            raise TimeoutError("handshake timeout")

    adapter.ib = _FailingIB()

    with pytest.raises(TimeoutError):
        await adapter._connect()

    assert adapter.client_id == 207
    assert adapter._connect_timeout_streak == 1
    gatekeeper.report_failure.assert_called_once()


@pytest.mark.asyncio
async def test_connect_cancelled_releases_gatekeeper_slot(monkeypatch):
    adapter = IBInsyncAdapter(client_id=207)
    gatekeeper = MagicMock()
    gatekeeper.acquire = AsyncMock(return_value=True)
    gatekeeper.report_aborted = MagicMock()
    adapter._reconnect_gatekeeper = gatekeeper

    class _CancelledIB:
        def isConnected(self):
            return False

        async def connectAsync(self, *args, **kwargs):
            raise asyncio.CancelledError()

    adapter.ib = _CancelledIB()

    with pytest.raises(asyncio.CancelledError):
        await adapter._connect()

    gatekeeper.report_aborted.assert_called_once()
    assert adapter._connect_gate_depth == 0


@pytest.mark.asyncio
async def test_connect_timeout_recreates_and_rebinds_ib_client(monkeypatch):
    adapter = IBInsyncAdapter(client_id=207)
    gatekeeper = MagicMock()
    gatekeeper.acquire = AsyncMock(return_value=True)
    gatekeeper.report_failure = MagicMock()
    adapter._reconnect_gatekeeper = gatekeeper

    class _FailingIB:
        def isConnected(self):
            return False

        async def connectAsync(self, *args, **kwargs):
            raise TimeoutError("handshake timeout")

    adapter.ib = _FailingIB()
    unbind = MagicMock()
    bind = MagicMock()
    monkeypatch.setattr(adapter, "_unbind_events", unbind)
    monkeypatch.setattr(adapter, "_bind_events", bind)

    with pytest.raises(TimeoutError):
        await adapter._connect()

    unbind.assert_called_once()
    bind.assert_called_once()


@pytest.mark.asyncio
async def test_start_is_idempotent_when_already_running(monkeypatch):
    adapter = IBInsyncAdapter()
    adapter._running = True
    created = []

    async def _connect():
        raise AssertionError("start should not reconnect when already running")

    monkeypatch.setattr(adapter, "_connect", _connect)
    monkeypatch.setattr(asyncio, "create_task", lambda coro, **kwargs: created.append(coro))

    await adapter.start()

    assert created == []


def test_legacy_global_adapter_requires_registered_shared_adapter(monkeypatch):
    fake_manager = MagicMock()
    fake_manager._exec_adapter = None

    monkeypatch.setattr(ib_adapter_mod, "_global_adapter", None)
    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: fake_manager,
    )

    with pytest.raises(RuntimeError, match="ib_global_adapter_unavailable_use_connection_manager"):
        ib_adapter_mod.get_ib_adapter()


def test_legacy_global_adapter_reuses_manager_exec_adapter(monkeypatch):
    fake_shared_adapter = MagicMock()
    fake_manager = MagicMock()
    fake_manager._exec_adapter = fake_shared_adapter
    fake_manager.reconnect_gatekeeper = object()

    monkeypatch.setattr(ib_adapter_mod, "_global_adapter", None)
    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: fake_manager,
    )

    adapter = ib_adapter_mod.get_ib_adapter()

    assert adapter is fake_shared_adapter


def test_legacy_global_adapter_returns_explicit_seeded_adapter(monkeypatch):
    seeded = MagicMock()
    monkeypatch.setattr(ib_adapter_mod, "_global_adapter", seeded)
    monkeypatch.setattr(
        "services.ibkr.ib_connection_manager.get_ib_connection_manager",
        lambda: MagicMock(_exec_adapter=None),
    )

    assert ib_adapter_mod.get_ib_adapter() is seeded


@pytest.mark.asyncio
async def test_owner_cancel_probe_is_throttled(monkeypatch):
    adapter = IBInsyncAdapter(client_id=207)
    adapter._aux_probe_sessions_enabled = True
    adapter._owner_probe_min_interval_sec = 60.0
    adapter._owner_probe_min_interval_rth_sec = 60.0
    adapter._reconnect_gatekeeper = None

    class _ProbeIB:
        connect_calls = 0

        async def connectAsync(self, *args, **kwargs):
            _ProbeIB.connect_calls += 1

        async def reqAllOpenOrdersAsync(self):
            return None

        def openTrades(self):
            return []

        def disconnect(self):
            return None

    monkeypatch.setattr("services.exec.adapters.ib_adapter.IB", _ProbeIB)

    ok1 = await adapter._cancel_via_owner_client(order_id=88, owner_client_id=1259)
    ok2 = await adapter._cancel_via_owner_client(order_id=88, owner_client_id=1259)

    assert ok1 is False
    assert ok2 is False
    assert _ProbeIB.connect_calls == 1


@pytest.mark.asyncio
async def test_owner_cancel_probe_not_throttled_in_rth(monkeypatch):
    adapter = IBInsyncAdapter(client_id=207)
    adapter._aux_probe_sessions_enabled = True
    adapter._owner_probe_min_interval_sec = 60.0
    adapter._owner_probe_min_interval_rth_sec = 1.0
    adapter._reconnect_gatekeeper = None
    monkeypatch.setattr(adapter, "_session_phase_et", lambda now=None: "RTH")

    class _ProbeIB:
        connect_calls = 0

        async def connectAsync(self, *args, **kwargs):
            _ProbeIB.connect_calls += 1

        async def reqAllOpenOrdersAsync(self):
            return None

        def openTrades(self):
            return []

        def disconnect(self):
            return None

    monkeypatch.setattr("services.exec.adapters.ib_adapter.IB", _ProbeIB)

    await adapter._cancel_via_owner_client(order_id=88, owner_client_id=1259)
    await adapter._cancel_via_owner_client(order_id=88, owner_client_id=1259)

    assert _ProbeIB.connect_calls == 1


@pytest.mark.asyncio
async def test_managed_mode_disables_sidecar_probe_and_owner_cancel():
    adapter = IBInsyncAdapter(client_id=207)
    adapter._aux_probe_sessions_enabled = True
    adapter.configure_managed_mode(enabled=True)

    owner_cancelled = await adapter._cancel_via_owner_client(order_id=88, owner_client_id=1259)
    probe = await adapter.probe_order_open_fresh(88)

    assert owner_cancelled is False
    assert probe["error"] == "managed_mode_probe_disabled"


@pytest.mark.asyncio
async def test_managed_mode_blocks_direct_connect_attempt():
    adapter = IBInsyncAdapter(client_id=207)
    adapter.configure_managed_mode(enabled=True)

    class _DisconnectedIB:
        def isConnected(self):
            return False

    adapter.ib = _DisconnectedIB()

    with pytest.raises(ConnectionError, match="ib_exec_connect_managed_by_connection_manager"):
        await adapter._connect()


@pytest.mark.asyncio
async def test_fresh_probe_dedupes_inflight_requests(monkeypatch):
    adapter = IBInsyncAdapter(client_id=207)
    adapter._aux_probe_sessions_enabled = True
    adapter._fresh_probe_min_interval_sec = 0.0
    adapter._fresh_probe_min_interval_rth_sec = 0.0
    adapter._reconnect_gatekeeper = None

    started = asyncio.Event()
    release = asyncio.Event()

    class _ProbeIB:
        connect_calls = 0

        async def connectAsync(self, *args, **kwargs):
            _ProbeIB.connect_calls += 1
            started.set()
            await release.wait()

        async def reqAllOpenOrdersAsync(self):
            return None

        def openTrades(self):
            return []

        def disconnect(self):
            return None

    monkeypatch.setattr("services.exec.adapters.ib_adapter.IB", _ProbeIB)

    t1 = asyncio.create_task(adapter.probe_order_open_fresh(88))
    await started.wait()
    t2 = asyncio.create_task(adapter.probe_order_open_fresh(88))
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert _ProbeIB.connect_calls == 1
    assert r1["probe_client_id"] == r2["probe_client_id"]


@pytest.mark.asyncio
async def test_owner_cancel_probe_dedupes_inflight_requests(monkeypatch):
    adapter = IBInsyncAdapter(client_id=207)
    adapter._aux_probe_sessions_enabled = True
    adapter._owner_probe_min_interval_sec = 0.0
    adapter._owner_probe_min_interval_rth_sec = 0.0
    adapter._reconnect_gatekeeper = None

    started = asyncio.Event()
    release = asyncio.Event()

    class _ProbeIB:
        connect_calls = 0

        async def connectAsync(self, *args, **kwargs):
            _ProbeIB.connect_calls += 1
            started.set()
            await release.wait()

        async def reqAllOpenOrdersAsync(self):
            return None

        def openTrades(self):
            return []

        def disconnect(self):
            return None

    monkeypatch.setattr("services.exec.adapters.ib_adapter.IB", _ProbeIB)

    t1 = asyncio.create_task(adapter._cancel_via_owner_client(order_id=88, owner_client_id=1259))
    await started.wait()
    t2 = asyncio.create_task(adapter._cancel_via_owner_client(order_id=88, owner_client_id=1259))
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert _ProbeIB.connect_calls == 1
    assert r1 is False
    assert r2 is False


@pytest.mark.asyncio
async def test_probe_order_open_fresh_disabled_by_default():
    adapter = IBInsyncAdapter(client_id=207)
    adapter._aux_probe_sessions_enabled = False

    out = await adapter.probe_order_open_fresh(99)

    assert out.get("checked") is False
    assert out.get("error") == "aux_probe_sessions_disabled"


def test_disconnected_reason_classified_from_recent_error(monkeypatch):
    adapter = IBInsyncAdapter()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.exec.adapters.ib_adapter.logger", mock_logger)

    adapter._on_error_event(1, 1100, "Connectivity between IB and Trader Workstation has been lost", None)
    adapter._on_disconnected_event()

    assert adapter._disconnect_count == 1
    assert adapter._last_disconnect_reason == "ib_connectivity_lost_1100"
    assert mock_logger.error.call_count >= 1


def test_disconnected_reason_not_overwritten_by_request_noise(monkeypatch):
    adapter = IBInsyncAdapter()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.exec.adapters.ib_adapter.logger", mock_logger)

    adapter._on_error_event(1, 1100, "Connectivity between IB and Trader Workstation has been lost", None)
    adapter._on_error_event(57, 354, "Requested market data is not subscribed", None)
    adapter._on_disconnected_event()

    assert adapter._last_disconnect_reason == "ib_connectivity_lost_1100"
    assert mock_logger.error.call_count >= 1


def test_order_warning_code_does_not_log_exec_error(monkeypatch):
    adapter = IBInsyncAdapter()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.exec.adapters.ib_adapter.logger", mock_logger)

    adapter._on_error_event(
        400,
        399,
        "Order Message: SELL 1 NVDA NASDAQ.NMS Warning: Your order will not be placed at the exchange until 09:30",
        None,
    )

    warning_events = [call.args[0] for call in mock_logger.warning.call_args_list if call.args]
    error_events = [call.args[0] for call in mock_logger.error.call_args_list if call.args]
    assert "ib_exec_order_warning_event" in warning_events
    assert "ib_exec_order_error_event" not in error_events


@pytest.mark.asyncio
async def test_exec_probe_updates_timestamps(monkeypatch):
    adapter = IBInsyncAdapter()
    adapter.ib = _FakeExecIBProbeOK()
    monkeypatch.setattr("services.exec.adapters.ib_adapter.set_ibkr_connection_up", lambda *args, **kwargs: None)
    monkeypatch.setattr("services.exec.adapters.ib_adapter.mark_ibkr_probe_ok", lambda *args, **kwargs: None)

    await adapter._probe_connection(source="test")

    assert adapter._last_probe_ok_ts > 0
    assert adapter._last_probe_latency_ms is not None
