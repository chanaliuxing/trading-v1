import asyncio
from datetime import datetime
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.ibkr.ib_connection_manager import IBConnectionManager
from services.ibkr.ib_connection_manager import IBReconnectGatekeeper
from services.ibkr.ib_connection_manager import ET
from services.ibkr.ib_connection_manager import _connect_ib_with_retry
from services.ibkr.ib_connection_manager import _default_exec_client_id
from services.ibkr.ib_connection_manager import _default_market_client_id
from services.ibkr.ib_connection_manager import _format_ib_exception
from services.ibkr.ib_connection_manager import _market_data_type_for_now
from services.ibkr.ib_connection_manager import _try_set_delayed_frozen_market_data
from services.ibkr import ib_gateway_circuit
from services.ibkr import ib_gateway_recovery_coordinator
from services.ibkr.ib_gateway_recovery_coordinator import run_authorized_gateway_recovery_proof
from services.ibkr.client_id_registry import get_client_id_registry


@pytest.fixture(autouse=True)
async def _shutdown_created_managers(monkeypatch):
    created: list[IBConnectionManager] = []
    original_init = IBConnectionManager.__init__

    def _tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(IBConnectionManager, "__init__", _tracked_init)
    try:
        yield
    finally:
        for manager in reversed(created):
            await manager.shutdown()


class _DummyContract:
    secType = "OPT"
    symbol = "QQQ"
    localSymbol = "QQQ  260309C00600000"
    exchange = "SMART"
    currency = "USD"
    lastTradeDateOrContractMonth = "20260309"
    right = "C"
    strike = 600.0
    multiplier = "100"
    tradingClass = "QQQ"
    conId = 123


class _DummyVixIndexContract:
    secType = "IND"
    symbol = "VIX"
    localSymbol = "VIX"
    exchange = "CBOE"
    currency = "USD"
    lastTradeDateOrContractMonth = ""
    right = ""
    strike = 0.0
    multiplier = ""
    tradingClass = ""
    conId = 13455763


def test_market_revalidation_state_changes(monkeypatch):
    manager = IBConnectionManager()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    manager._handle_market_error_event(1, 1100, "Connectivity between IB and Trader Workstation has been lost", None)
    snap = manager.get_market_health_snapshot()
    assert snap["degraded"] is True
    assert snap["revalidate_required"] is True
    assert snap["revalidate_reason"] == "IB_1100"

    manager._handle_market_error_event(1, 1102, "Connectivity restored - data maintained", None)
    snap = manager.get_market_health_snapshot()
    assert snap["degraded"] is False
    assert snap["revalidate_required"] is True
    assert snap["revalidate_reason"] == "IB_1102"

    manager.mark_market_revalidated(source="test", ok=True, detail="OK")
    snap = manager.get_market_health_snapshot()
    assert snap["degraded"] is False
    assert snap["revalidate_required"] is False


def test_market_disconnect_reason_uses_recent_error_context(monkeypatch):
    manager = IBConnectionManager()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    manager._handle_market_error_event(1, 1100, "Connectivity between IB and Trader Workstation has been lost", None)
    manager._handle_market_disconnected()

    snap = manager.get_market_health_snapshot()
    assert snap["disconnect_count"] == 1
    assert snap["last_disconnect_reason"] == "ib_connectivity_lost_1100"
    assert snap["last_disconnect_source"] == "recent_connectivity_error"


def test_market_disconnect_reason_not_overwritten_by_request_noise(monkeypatch):
    manager = IBConnectionManager()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    manager._handle_market_error_event(1, 1100, "Connectivity between IB and Trader Workstation has been lost", None)
    manager._handle_market_error_event(57, 354, "Requested market data is not subscribed", _DummyContract())
    manager._handle_market_disconnected()

    snap = manager.get_market_health_snapshot()
    assert snap["last_disconnect_reason"] == "ib_connectivity_lost_1100"
    assert snap["last_disconnect_source"] == "recent_connectivity_error"


def test_offhours_connectivity_logs_are_throttled(monkeypatch):
    manager = IBConnectionManager()
    manager._offhours_connectivity_log_cooldown_sec = 300.0
    monkeypatch.setattr(manager, "_is_rth_et", lambda **_kwargs: False)

    assert manager._should_emit_offhours_session_state_log(now_ts=1000.0, error_code=1100) is True
    assert manager._should_emit_offhours_session_state_log(now_ts=1100.0, error_code=1100) is False
    assert manager._should_emit_offhours_session_state_log(now_ts=1301.0, error_code=1100) is True


def test_rth_connectivity_logs_not_throttled(monkeypatch):
    manager = IBConnectionManager()
    manager._offhours_connectivity_log_cooldown_sec = 300.0
    monkeypatch.setattr(manager, "_is_rth_et", lambda **_kwargs: True)

    assert manager._should_emit_offhours_session_state_log(now_ts=1000.0, error_code=1100) is True
    assert manager._should_emit_offhours_session_state_log(now_ts=1100.0, error_code=1100) is True


def test_offhours_1102_recovery_log_bypasses_throttle(monkeypatch):
    manager = IBConnectionManager()
    manager._offhours_connectivity_log_cooldown_sec = 300.0
    monkeypatch.setattr(manager, "_is_rth_et", lambda **_kwargs: False)

    assert manager._should_emit_offhours_session_state_log(now_ts=1000.0, error_code=1100) is True
    assert manager._should_emit_offhours_session_state_log(now_ts=1100.0, error_code=1100) is False
    assert manager._should_emit_offhours_session_state_log(now_ts=1120.0, error_code=1102) is True


def test_gatekeeper_connection_refused_tracks_readiness_not_connect_failures():
    gatekeeper = IBReconnectGatekeeper()

    gatekeeper.report_failure(
        caller="exec_adapter_connect",
        error="[Errno 61] Connect call failed ('127.0.0.1', 4002)",
    )

    assert gatekeeper._consecutive_failures == 0
    assert gatekeeper._readiness_failures == 1


def test_format_ib_exception_preserves_empty_timeout_type():
    assert _format_ib_exception(TimeoutError()) == "TimeoutError: TimeoutError()"


def test_gatekeeper_empty_timeout_gets_severe_backoff():
    gatekeeper = IBReconnectGatekeeper()

    gatekeeper.report_failure(caller="market", error=_format_ib_exception(TimeoutError()))

    assert gatekeeper._consecutive_failures == 2
    assert gatekeeper._readiness_failures == 0


def test_gatekeeper_report_failure_does_not_release_other_owner_slot():
    gatekeeper = IBReconnectGatekeeper()

    async def _run():
        await gatekeeper._attempt_slot.acquire()
        gatekeeper._attempt_owner = "market"
        gatekeeper._attempt_started_ts = 1000.0

        gatekeeper.report_failure(
            caller="exec",
            error="TimeoutError: ib_api_handshake_not_ready",
            host="127.0.0.1",
            port=4002,
        )

        assert gatekeeper._attempt_slot.locked() is True
        assert gatekeeper._attempt_owner == "market"

        gatekeeper.report_failure(
            caller="market",
            error="TimeoutError: connectAsync timed out",
            host="127.0.0.1",
            port=4002,
        )
        assert gatekeeper._attempt_slot.locked() is False
        assert gatekeeper._attempt_owner is None

    asyncio.run(_run())


def test_api_readiness_probe_fallbacks_have_safe_default():
    manager = IBConnectionManager()

    assert manager._api_readiness_probe_client_id_fallbacks >= 1


def test_runtime_recovery_timeout_arms_short_circuit():
    manager = IBConnectionManager()
    manager._ensure_runtime_recovery_task = MagicMock()

    manager._trigger_runtime_recovery(reason=_format_ib_exception(TimeoutError()), source="unit_test")

    assert manager._recovery_short_circuit_remaining_sec() > 0
    assert "TimeoutError" in manager._recovery_short_circuit_reason


@pytest.mark.asyncio
async def test_gatekeeper_clears_readiness_backoff_when_port_is_ready(monkeypatch):
    gatekeeper = IBReconnectGatekeeper()
    gatekeeper._last_attempt_ts = 1000.0
    gatekeeper._readiness_failures = 3
    monkeypatch.setattr("services.ibkr.ib_connection_manager.time.time", lambda: 1001.0)
    monkeypatch.setattr(gatekeeper, "tcp_probe", AsyncMock(return_value=True))

    allowed = await gatekeeper.acquire(caller="market", host="127.0.0.1", port=4002)

    assert allowed is True
    assert gatekeeper._readiness_failures == 0
    assert gatekeeper._consecutive_failures == 0
    gatekeeper.report_aborted(caller="market", reason="test_cleanup")


@pytest.mark.asyncio
async def test_wait_until_ready_probes_and_clears_refused_short_circuit(monkeypatch):
    manager = IBConnectionManager()
    manager._recovery_short_circuit_until_ts = 10**12
    manager._recovery_short_circuit_reason = "[Errno 61] Connect call failed ('127.0.0.1', 4002)"
    manager._recovery_short_circuit_source = "unit_test"
    manager.market_connected = MagicMock(side_effect=[False, True])
    manager.exec_connected = MagicMock(return_value=True)
    manager.master_connected = MagicMock(return_value=True)
    manager._master_truth_complete = True
    manager._gatekeeper.clear_failure_backoff = MagicMock()
    manager._probe_api_handshake_ready = AsyncMock(return_value=True)

    market_ready, exec_ready = await manager.wait_until_ready(
        host="127.0.0.1",
        port=4002,
        account="DU123",
        require_exec=True,
        require_master=True,
        timeout_sec=1.0,
        initiator="unit_test",
    )

    assert market_ready is True
    assert exec_ready is True
    assert manager._recovery_short_circuit_remaining_sec() == 0.0
    manager._probe_api_handshake_ready.assert_awaited_once_with(
        caller="unit_test:circuit_recovery_probe",
        host="127.0.0.1",
        port=4002,
    )
    manager._gatekeeper.clear_failure_backoff.assert_called_once_with(
        caller="unit_test:circuit_recovery_probe",
        reason="local_recovery_short_circuit_probe_ready",
    )


@pytest.mark.asyncio
async def test_wait_until_ready_uses_short_cadence_when_short_circuit_probe_fails(monkeypatch):
    manager = IBConnectionManager()
    sleeps: list[float] = []
    now = {"value": 1000.0}

    async def _record_sleep(delay):
        sleeps.append(float(delay))
        now["value"] += float(delay)

    monkeypatch.setattr("services.ibkr.ib_connection_manager.time.time", lambda: now["value"])
    monkeypatch.setattr("services.ibkr.ib_connection_manager.asyncio.sleep", _record_sleep)
    manager._recovery_short_circuit_until_ts = now["value"] + 10**6
    manager._recovery_short_circuit_reason = "TimeoutError: TimeoutError()"
    manager._recovery_short_circuit_source = "unit_test"
    manager._gateway_circuit_recovery_probe_sleep_sec = 2.0
    manager._gatekeeper.clear_failure_backoff = MagicMock()
    manager._probe_api_handshake_ready = AsyncMock(return_value=False)

    market_ready, exec_ready = await manager.wait_until_ready(
        host="127.0.0.1",
        port=4002,
        require_exec=False,
        require_master=False,
        timeout_sec=1.0,
        initiator="unit_test",
    )

    assert market_ready is False
    assert exec_ready is True
    assert manager._recovery_short_circuit_remaining_sec() > 0.0
    manager._probe_api_handshake_ready.assert_awaited_once_with(
        caller="unit_test:circuit_recovery_probe",
        host="127.0.0.1",
        port=4002,
    )
    assert sleeps == [pytest.approx(1.0)]
    manager._gatekeeper.clear_failure_backoff.assert_not_called()


@pytest.mark.asyncio
async def test_get_market_ib_blocks_all_connects_during_short_circuit(monkeypatch):
    manager = IBConnectionManager()
    manager._recovery_short_circuit_until_ts = 10**12
    manager._recovery_short_circuit_reason = "ConnectionRefusedError: [Errno 61] Connect call failed"
    manager._recovery_short_circuit_source = "unit_test"
    manager._gatekeeper.probe_ready = AsyncMock(return_value=True)
    manager._probe_api_handshake_ready = AsyncMock(return_value=False)
    manager._gatekeeper.acquire = AsyncMock(return_value=True)

    with pytest.raises(ConnectionError, match="ib_recovery_short_circuit_active"):
        await manager.get_market_ib(host="127.0.0.1", port=4002)

    manager._gatekeeper.probe_ready.assert_not_awaited()
    manager._probe_api_handshake_ready.assert_not_awaited()
    manager._gatekeeper.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_until_ready_does_not_emit_timeout_when_ready_at_deadline(monkeypatch):
    manager = IBConnectionManager()
    manager.market_connected = lambda: True
    manager.exec_connected = lambda: True
    manager.master_connected = lambda: True
    manager._master_truth_complete = True
    manager._runtime_recovery_active = False

    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    market_ready, exec_ready = await manager.wait_until_ready(
        host="127.0.0.1",
        port=4002,
        require_exec=True,
        require_master=True,
        timeout_sec=0.0,
        initiator="unit_test",
    )

    assert market_ready is True
    assert exec_ready is True
    mock_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_wait_until_ready_stages_market_before_exec_and_master(monkeypatch):
    manager = IBConnectionManager()
    calls: list[str] = []
    now = {"value": 1000.0}

    def _market_connected():
        return "market" in calls

    def _exec_connected():
        return "exec" in calls

    def _master_connected():
        return "master" in calls

    async def _sleep(delay):
        calls.append(f"sleep:{delay}")
        now["value"] += float(delay)

    async def _get_market(**_kwargs):
        calls.append("market")

    async def _get_exec(**_kwargs):
        calls.append("exec")

    async def _get_master(**_kwargs):
        calls.append("master")
        manager._master_truth_complete = True

    monkeypatch.setattr("services.ibkr.ib_connection_manager.time.time", lambda: now["value"])
    monkeypatch.setattr("services.ibkr.ib_connection_manager.asyncio.sleep", _sleep)
    manager.market_connected = _market_connected
    manager.exec_connected = _exec_connected
    manager.master_connected = _master_connected
    manager.get_market_ib = AsyncMock(side_effect=_get_market)
    manager.get_exec_adapter = AsyncMock(side_effect=_get_exec)
    manager.get_master_ib = AsyncMock(side_effect=_get_master)
    manager.refresh_master_truth_snapshot = AsyncMock()
    manager._runtime_recovery_active = False

    market_ready, exec_ready = await manager.wait_until_ready(
        host="127.0.0.1",
        port=4002,
        account="DU123",
        require_exec=True,
        require_master=True,
        timeout_sec=5.0,
        initiator="unit_test",
    )

    assert market_ready is True
    assert exec_ready is True
    assert calls[:5] == ["market", "sleep:1.0", "exec", "sleep:1.0", "master"]


@pytest.mark.asyncio
async def test_runtime_recovery_loop_emits_broker_api_ready_when_fully_ready():
    manager = IBConnectionManager()
    events = []

    async def _listener(payload):
        events.append(dict(payload))

    manager.register_health_event_listener("unit_test", _listener)
    manager.wait_until_ready = AsyncMock(return_value=(True, True))
    manager.master_connected = lambda: True
    manager._master_truth_complete = True

    manager._trigger_runtime_recovery(reason="unit_test_disconnect", source="unit_test")

    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if any(evt.get("event_type") == "broker_api_ready" for evt in events):
            break
        await asyncio.sleep(0.01)

    ready = next(evt for evt in events if evt.get("event_type") == "broker_api_ready")
    assert ready["reason"] == "ib_runtime_recovery_api_ready"
    assert ready["resume_ready"] is False
    assert ready["broker_truth_complete"] is False
    assert ready["position_ledger_clean"] is False
    assert ready["requires_runtime_reconciler"] is True


@pytest.mark.asyncio
async def test_get_market_ib_default_bypasses_api_preflight_probe(monkeypatch):
    manager = IBConnectionManager()
    manager._probe_api_handshake_ready = AsyncMock(return_value=False)
    manager._gatekeeper.acquire = AsyncMock(return_value=True)
    manager._gatekeeper.report_failure = MagicMock()
    manager._gatekeeper.report_success = MagicMock()
    manager._gatekeeper.report_aborted = MagicMock()
    connected_ib = MagicMock()
    connected_ib.isConnected.return_value = True
    fallback_counts = []

    async def _fake_connect(ib, host, port, client_id, **kwargs):
        fallback_counts.append(kwargs.get("timeout_client_id_fallbacks"))
        return connected_ib, client_id

    monkeypatch.setattr("services.ibkr.ib_connection_manager._connect_ib_with_retry", _fake_connect)
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: MagicMock())
    monkeypatch.setattr("services.ibkr.ib_connection_manager._try_set_delayed_frozen_market_data", lambda ib: None)
    monkeypatch.setattr(manager, "_bind_market_events", lambda: None)
    monkeypatch.setattr(manager, "_probe_market_connection", AsyncMock())
    monkeypatch.setattr(manager, "_ensure_market_probe_task", lambda: None)

    out = await manager.get_market_ib(host="127.0.0.1", port=4002)

    assert out is connected_ib
    manager._probe_api_handshake_ready.assert_not_awaited()
    manager._gatekeeper.acquire.assert_awaited_once()
    manager._gatekeeper.report_failure.assert_not_called()
    manager._gatekeeper.report_success.assert_called_once_with(caller="market")
    manager._gatekeeper.report_aborted.assert_not_called()
    assert manager._recovery_short_circuit_remaining_sec() == 0


@pytest.mark.asyncio
async def test_get_market_ib_blocks_real_connect_after_api_preflight_timeout_when_enabled(monkeypatch):
    manager = IBConnectionManager()
    manager._pre_connect_api_probe_enabled = True
    manager._probe_api_handshake_ready = AsyncMock(return_value=False)
    manager._gatekeeper.acquire = AsyncMock(return_value=True)
    manager._gatekeeper.report_failure = MagicMock()
    manager._gatekeeper.report_aborted = MagicMock()
    connect_mock = AsyncMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager._connect_ib_with_retry", connect_mock)

    with pytest.raises(TimeoutError, match="ib_api_handshake_not_ready"):
        await manager.get_market_ib(host="127.0.0.1", port=4002)

    manager._probe_api_handshake_ready.assert_awaited_once()
    manager._gatekeeper.acquire.assert_not_awaited()
    connect_mock.assert_not_awaited()
    manager._gatekeeper.report_failure.assert_not_called()
    manager._gatekeeper.report_aborted.assert_called_once_with(
        caller="market",
        reason="TimeoutError: ib_api_handshake_not_ready",
    )
    assert manager._recovery_short_circuit_remaining_sec() > 0


@pytest.mark.asyncio
async def test_market_disconnect_emits_health_pause_event():
    manager = IBConnectionManager()
    events = []

    async def _listener(payload):
        events.append(dict(payload))

    manager.register_health_event_listener("unit_test", _listener)
    manager.wait_until_ready = AsyncMock(return_value=(True, True))
    manager.master_connected = lambda: True
    manager.master_truth_complete = lambda: True
    manager._market_ib = _FakeIBProbeOK()
    manager._handle_market_disconnected()
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if any(evt.get("event_type") == "broker_api_ready" for evt in events):
            break
        await asyncio.sleep(0.01)

    assert [evt.get("event_type") for evt in events[:2]] == ["ib_disconnected", "broker_api_ready"]
    assert events[0]["reason"] == "ib_market_disconnected"
    assert events[0]["source"] == "market_disconnect_event"


@pytest.mark.asyncio
async def test_master_disconnect_emits_health_pause_event():
    manager = IBConnectionManager()
    events = []

    async def _listener(payload):
        events.append(dict(payload))

    manager.register_health_event_listener("unit_test", _listener)
    manager.wait_until_ready = AsyncMock(return_value=(True, True))
    manager.master_connected = lambda: True
    manager.master_truth_complete = lambda: True
    manager._master_ib = _FakeIBProbeOK()
    manager._handle_master_disconnected()
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if any(evt.get("event_type") == "broker_api_ready" for evt in events):
            break
        await asyncio.sleep(0.01)

    assert [evt.get("event_type") for evt in events[:2]] == ["ib_disconnected", "broker_api_ready"]
    assert events[0]["reason"] == "ib_master_disconnected"
    assert events[0]["source"] == "master_disconnect_event"


@pytest.mark.asyncio
async def test_api_handshake_probe_respects_failure_cooldown(monkeypatch):
    manager = IBConnectionManager()
    manager._api_readiness_probe_next_allowed_ts = 2000.0
    monkeypatch.setattr("services.ibkr.ib_connection_manager.time.time", lambda: 1990.0)
    ib_factory = MagicMock(side_effect=AssertionError("probe must not open a socket during cooldown"))
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", ib_factory)

    ready = await manager._probe_api_handshake_ready(
        caller="unit_test",
        host="127.0.0.1",
        port=4002,
    )

    assert ready is False
    ib_factory.assert_not_called()


@pytest.mark.asyncio
async def test_api_handshake_probe_rejects_unauthorized_recovery_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "market:pre_connect_api_probe")
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    manager = IBConnectionManager()
    ib_factory = MagicMock(side_effect=AssertionError("unauthorized caller must not open a socket"))
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", ib_factory)

    try:
        ready = await manager._probe_api_handshake_ready(
            caller="external:circuit_recovery_probe",
            host="127.0.0.1",
            port=4002,
        )

        assert ready is False
        ib_factory.assert_not_called()
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_repeated_api_handshake_failures_emit_gateway_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        manager = IBConnectionManager()
        manager._api_readiness_probe_unhealthy_threshold = 1
        manager._api_readiness_probe_unhealthy_min_duration_sec = 0.0
        manager._api_readiness_probe_failure_cooldown_sec = 0.0
        manager._api_readiness_probe_unhealthy_emit_cooldown_sec = 1.0
        events = []

        async def _listener(payload):
            events.append(dict(payload))

        class _TimeoutProbe:
            async def connectAsync(self, host, port, clientId, timeout):
                raise TimeoutError()

            def disconnect(self):
                return None

        manager.register_health_event_listener("unit_test", _listener)
        monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", _TimeoutProbe)

        assert await manager._probe_api_handshake_ready(caller="market", host="127.0.0.1", port=4002) is False
        await asyncio.sleep(0)

        event = next(evt for evt in events if evt.get("event_type") == "ib_gateway_api_unhealthy")
        assert event["reason"] == "gateway_api_half_open"
        assert event["action"] == "manual_gateway_api_recovery_required"
        assert event["failure_streak"] == 1
        circuit_state = ib_gateway_circuit.get_ib_gateway_connection_circuit().snapshot()
        assert circuit_state.open is True
        assert circuit_state.manual_required is True
        assert circuit_state.reason == "manual_gateway_api_recovery_required"
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_connection_refused_probe_does_not_mark_manual_required(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    try:
        manager = IBConnectionManager()
        manager._api_readiness_probe_unhealthy_threshold = 1
        manager._api_readiness_probe_unhealthy_min_duration_sec = 0.0
        manager._api_readiness_probe_failure_cooldown_sec = 0.0
        events = []

        async def _listener(payload):
            events.append(dict(payload))

        class _ConnectionRefusedProbe:
            async def connectAsync(self, host, port, clientId, timeout):
                raise ConnectionRefusedError("[Errno 61] Connect call failed ('127.0.0.1', 4002)")

            def disconnect(self):
                return None

        manager.register_health_event_listener("unit_test", _listener)
        monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", _ConnectionRefusedProbe)

        assert await manager._probe_api_handshake_ready(caller="market", host="127.0.0.1", port=4002) is False
        await asyncio.sleep(0)

        assert not any(evt.get("event_type") == "ib_gateway_api_unhealthy" for evt in events)
        circuit_state = ib_gateway_circuit.get_ib_gateway_connection_circuit().snapshot()
        assert circuit_state.open is True
        assert circuit_state.manual_required is False
        assert "ConnectionRefusedError" in circuit_state.reason
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


def test_planned_restart_connection_refused_within_grace_does_not_mark_manual_required(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
        circuit.declare_planned_restart(source="operator_restart", grace_sec=120, now_ts=1000.0)

        state = circuit.record_failure(
            reason="ConnectionRefusedError: [Errno 61] Connect call failed ('127.0.0.1', 4002)",
            source="market:pre_connect_api_probe",
            now_ts=1010.0,
            metadata={"host": "127.0.0.1", "port": 4002},
        )

        assert state.open is True
        assert state.manual_required is False
        assert state.state == "recoverable_restarting"
        assert state.reason == "gateway_restarting"
    finally:
        ib_gateway_circuit._CIRCUIT = None


def test_connection_refused_outside_grace_enters_recoverable_non_manual_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
        circuit.declare_planned_restart(source="operator_restart", grace_sec=5, now_ts=1000.0)

        state = circuit.record_failure(
            reason="ConnectionRefusedError: [Errno 61] Connect call failed ('127.0.0.1', 4002)",
            source="market:pre_connect_api_probe",
            now_ts=1010.0,
            metadata={"host": "127.0.0.1", "port": 4002},
        )

        assert state.open is True
        assert state.manual_required is False
        assert state.state == "recoverable_refused"
        assert "ConnectionRefusedError" in state.reason
    finally:
        ib_gateway_circuit._CIRCUIT = None


def test_runtime_failure_preserves_last_operator_proof_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
        circuit.record_recovery_proof_success(
            source="operator_cli",
            reason="authorized_gateway_recovery_proof_succeeded",
            now_ts=1000.0,
        )

        state = circuit.record_failure(
            reason="TimeoutError: TimeoutError()",
            source="market",
            now_ts=1010.0,
            metadata={"client_id": 10},
        )

        assert state.state == "recoverable_cooldown"
        assert state.last_proof_status == "PROOF_SUCCEEDED"
        assert state.last_proof_reason == "authorized_gateway_recovery_proof_succeeded"
        assert state.reason == "TimeoutError: TimeoutError()"
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_tcp_open_handshake_timeout_can_mark_gateway_api_half_open(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        manager = IBConnectionManager()
        manager._api_readiness_probe_unhealthy_threshold = 1
        manager._api_readiness_probe_unhealthy_min_duration_sec = 0.0
        manager._api_readiness_probe_failure_cooldown_sec = 0.0

        class _TcpOpenApiTimeoutProbe:
            async def connectAsync(self, host, port, clientId, timeout):
                return None

            async def reqCurrentTimeAsync(self):
                raise TimeoutError("ib_api_handshake_not_ready")

            def disconnect(self):
                return None

        monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", _TcpOpenApiTimeoutProbe)

        assert await manager._probe_api_handshake_ready(caller="market", host="127.0.0.1", port=4002) is False

        circuit_state = ib_gateway_circuit.get_ib_gateway_connection_circuit().snapshot()
        assert circuit_state.open is True
        assert circuit_state.manual_required is True
        assert circuit_state.state == "manual_required"
        assert circuit_state.reason == "manual_gateway_api_recovery_required"
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_manual_required_refusal_latch_allows_authorized_recovery_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv(
        "IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES",
        "manager_runtime_recovery:circuit_recovery_probe",
    )
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.mark_manual_required(
        reason="manual_gateway_api_recovery_required",
        source="market:pre_connect_api_probe",
        metadata={
            "gateway_reason": "gateway_api_half_open",
            "error": "ConnectionRefusedError: [Errno 61] Connect call failed ('127.0.0.1', 4002)",
        },
    )
    assert circuit.snapshot().manual_required is True

    manager = IBConnectionManager()
    fake_ib = _FakeIBProbeConnectOK()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: fake_ib)

    try:
        ready = await manager._probe_api_handshake_ready(
            caller="manager_runtime_recovery:circuit_recovery_probe",
            host="127.0.0.1",
            port=4002,
        )

        assert ready is True
        assert fake_ib.connect_calls == 1
        assert fake_ib.disconnect_calls == 1
        circuit_state = circuit.snapshot()
        assert circuit_state.open is False
        assert circuit_state.manual_required is False
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_manual_required_half_open_still_blocks_recovery_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv(
        "IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES",
        "manager_runtime_recovery:circuit_recovery_probe",
    )
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.mark_manual_required(
        reason="manual_gateway_api_recovery_required",
        source="market:pre_connect_api_probe",
        metadata={
            "gateway_reason": "gateway_api_half_open",
            "error": "TimeoutError: ib_api_handshake_not_ready",
        },
    )
    manager = IBConnectionManager()
    ib_factory = MagicMock(side_effect=AssertionError("true half-open manual latch must not open a socket"))
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", ib_factory)

    try:
        ready = await manager._probe_api_handshake_ready(
            caller="manager_runtime_recovery:circuit_recovery_probe",
            host="127.0.0.1",
            port=4002,
        )

        assert ready is False
        ib_factory.assert_not_called()
        circuit_state = circuit.snapshot()
        assert circuit_state.open is True
        assert circuit_state.manual_required is True
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_api_handshake_unhealthy_waits_for_min_duration(monkeypatch):
    manager = IBConnectionManager()
    manager._api_readiness_probe_unhealthy_threshold = 2
    manager._api_readiness_probe_unhealthy_min_duration_sec = 600.0
    manager._api_readiness_probe_failure_cooldown_sec = 0.0
    events = []

    async def _listener(payload):
        events.append(dict(payload))

    class _TimeoutProbe:
        async def connectAsync(self, host, port, clientId, timeout):
            raise TimeoutError()

        def disconnect(self):
            return None

    manager.register_health_event_listener("unit_test", _listener)
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", _TimeoutProbe)
    monkeypatch.setattr("services.ibkr.ib_connection_manager.time.time", lambda: 1000.0)
    assert await manager._probe_api_handshake_ready(caller="market", host="127.0.0.1", port=4002) is False
    manager._api_readiness_probe_next_allowed_ts = 0.0
    monkeypatch.setattr("services.ibkr.ib_connection_manager.time.time", lambda: 1060.0)
    assert await manager._probe_api_handshake_ready(caller="market", host="127.0.0.1", port=4002) is False
    await asyncio.sleep(0)

    assert not any(evt.get("event_type") == "ib_gateway_api_unhealthy" for evt in events)


@pytest.mark.asyncio
async def test_api_handshake_probe_is_disposable_and_disconnects(monkeypatch):
    manager = IBConnectionManager()
    manager._api_readiness_probe_release_grace_sec = 0.0
    fake_ib = _FakeIBProbeConnectOK()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: fake_ib)

    ready = await manager._probe_api_handshake_ready(
        caller="market:pre_connect_api_probe",
        host="127.0.0.1",
        port=4002,
    )

    assert ready is True
    assert fake_ib.disconnect_calls == 1


@pytest.mark.asyncio
async def test_api_handshake_probe_does_not_clear_open_gateway_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.record_failure(
        reason="TimeoutError: ib_api_handshake_not_ready",
        source="market",
        metadata={"host": "127.0.0.1", "port": 4002},
    )
    assert circuit.snapshot().open is True

    manager = IBConnectionManager()
    fake_ib = _FakeIBProbeConnectOK()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: fake_ib)

    try:
        ready = await manager._probe_api_handshake_ready(
            caller="external:circuit_recovery_probe",
            host="127.0.0.1",
            port=4002,
        )

        assert ready is False
        assert fake_ib.disconnect_calls == 0
        assert circuit.snapshot().open is True
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_api_handshake_probe_denied_by_coordinator_sets_local_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv(
        "IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES",
        "manager_runtime_recovery:circuit_recovery_probe",
    )
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.record_failure(
        reason="TimeoutError: ib_api_handshake_not_ready",
        source="market",
        metadata={"host": "127.0.0.1", "port": 4002},
    )

    manager = IBConnectionManager()
    manager._api_readiness_probe_failure_cooldown_sec = 60.0
    ib_factory = MagicMock(side_effect=AssertionError("denied recovery probe must not construct IB"))
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", ib_factory)

    try:
        first = await manager._probe_api_handshake_ready(
            caller="external:circuit_recovery_probe",
            host="127.0.0.1",
            port=4002,
        )
        second = await manager._probe_api_handshake_ready(
            caller="external:circuit_recovery_probe",
            host="127.0.0.1",
            port=4002,
        )

        assert first is False
        assert second is False
        assert manager._api_readiness_probe_next_allowed_ts > 0.0
        ib_factory.assert_not_called()
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_wait_until_ready_uses_short_probe_cadence_when_circuit_recoverable(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_BASE_COOLDOWN_SEC", "30")
    ib_gateway_circuit._CIRCUIT = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.record_failure(
        reason="TimeoutError: ib_api_handshake_not_ready",
        source="market",
        now_ts=1000.0,
        metadata={"host": "127.0.0.1", "port": 4002},
    )

    manager = IBConnectionManager()
    manager._runtime_recovery_active = False
    manager._gateway_circuit_recovery_probe_sleep_sec = 2.0
    manager._probe_api_handshake_ready = AsyncMock(return_value=False)
    now = {"value": 1001.0}
    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(float(delay))
        now["value"] = 2000.0

    monkeypatch.setattr("services.ibkr.ib_connection_manager.time.time", lambda: now["value"])
    monkeypatch.setattr("services.ibkr.ib_connection_manager.asyncio.sleep", _record_sleep)

    try:
        market_ready, exec_ready = await manager.wait_until_ready(
            host="127.0.0.1",
            port=4002,
            require_exec=True,
            require_master=True,
            timeout_sec=90.0,
            initiator="manager_runtime_recovery",
        )

        assert market_ready is False
        assert exec_ready is False
        assert sleeps
        assert sleeps[0] == pytest.approx(2.0)
        manager._probe_api_handshake_ready.assert_awaited_once()
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_wait_until_ready_probes_gateway_circuit_open_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv(
        "IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES",
        "manager_runtime_recovery:circuit_recovery_probe",
    )
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.record_failure(
        reason="TimeoutError: ib_api_handshake_not_ready",
        source="market",
        metadata={"host": "127.0.0.1", "port": 4002},
    )

    manager = IBConnectionManager()
    manager.market_connected = MagicMock(side_effect=[False, True])
    manager.exec_connected = MagicMock(return_value=True)
    manager.master_connected = MagicMock(return_value=True)
    manager._master_truth_complete = True
    manager._runtime_recovery_active = False
    manager._gatekeeper.clear_failure_backoff = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.asyncio.sleep", AsyncMock())

    async def _probe_ready(**_kwargs):
        circuit.record_success(source="manager_runtime_recovery:circuit_recovery_probe")
        return True

    manager._probe_api_handshake_ready = AsyncMock(side_effect=_probe_ready)

    try:
        market_ready, exec_ready = await manager.wait_until_ready(
            host="127.0.0.1",
            port=4002,
            require_exec=True,
            require_master=True,
            timeout_sec=1.0,
            initiator="manager_runtime_recovery",
        )

        assert market_ready is True
        assert exec_ready is True
        manager._probe_api_handshake_ready.assert_awaited_once_with(
            caller="manager_runtime_recovery:circuit_recovery_probe",
            host="127.0.0.1",
            port=4002,
        )
        manager._gatekeeper.clear_failure_backoff.assert_called_once_with(
            caller="manager_runtime_recovery:circuit_recovery_probe",
            reason="gateway_circuit_recovery_probe_ready",
        )
        assert circuit.snapshot().open is False
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_api_handshake_probe_retries_next_client_id_when_probe_id_is_busy(monkeypatch):
    manager = IBConnectionManager()
    manager._api_readiness_probe_client_id = 97
    manager._api_readiness_probe_client_id_fallbacks = 2
    manager._api_readiness_probe_release_grace_sec = 0.0
    first = _FakeIBProbeClientIdInUse()
    second = _FakeIBProbeConnectOK()
    created = [first, second]
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: created.pop(0))

    ready = await manager._probe_api_handshake_ready(
        caller="market:pre_connect_api_probe",
        host="127.0.0.1",
        port=4002,
    )

    assert ready is True
    assert first.client_ids == [97]
    assert second.client_ids == [98]
    assert first.disconnect_calls == 1
    assert second.disconnect_calls == 1


@pytest.mark.asyncio
async def test_authorized_operator_proof_clears_manual_required_after_time_positions_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "operator:manual_proof")
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.mark_manual_required(
        reason="manual_gateway_api_recovery_required",
        source="market:pre_connect_api_probe",
        metadata={"gateway_reason": "gateway_api_half_open", "error": "TimeoutError: ib_api_handshake_not_ready"},
    )

    try:
        result = await run_authorized_gateway_recovery_proof(
            source="operator:manual_proof",
            ack_token="ACK-123",
            now=datetime.fromisoformat("2026-05-12T10:00:00+00:00"),
            host="127.0.0.1",
            port=4002,
            ib_factory=_FakeIBProofOK,
        )

        assert result.status == "PROOF_SUCCEEDED"
        assert result.current_time_ok is True
        assert result.positions_ok is True
        assert result.open_orders_ok is True
        assert result.ib_constructed is True
        circuit_state = circuit.snapshot()
        assert circuit_state.open is False
        assert circuit_state.manual_required is False
        assert circuit_state.state == "closed"
        assert circuit_state.reason == "authorized_gateway_recovery_proof_succeeded"
        assert circuit_state.source == "operator:manual_proof"
        assert circuit_state.last_proof_status == "PROOF_SUCCEEDED"
        assert circuit_state.last_proof_reason == "authorized_gateway_recovery_proof_succeeded"
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_pre_connect_probe_skips_after_recent_operator_proof_success(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.record_recovery_proof_success(
        source="operator_cli",
        reason="authorized_gateway_recovery_proof_succeeded",
    )
    manager = IBConnectionManager()
    manager._pre_connect_api_probe_enabled = True
    manager._operator_recovery_proof_ok_ttl_sec = 120
    manager._probe_api_handshake_ready = AsyncMock(side_effect=AssertionError("recent operator proof should be reused"))

    try:
        await manager._ensure_api_ready_before_real_connect(
            caller="market",
            host="127.0.0.1",
            port=4002,
        )

        manager._probe_api_handshake_ready.assert_not_awaited()
        assert manager._api_readiness_probe_recently_ok() is True
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_stale_pre_connect_probe_manual_circuit_is_cleared_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
        circuit.mark_manual_required(
            reason="manual_gateway_api_recovery_required",
            source="market:pre_connect_api_probe",
            metadata={"gateway_reason": "gateway_api_half_open"},
        )
        manager = IBConnectionManager()

        await manager._ensure_recovery_short_circuit_allows_connect(
            caller="market",
            host="127.0.0.1",
            port=4002,
        )

        state = circuit.snapshot()
        assert state.open is False
        assert state.manual_required is False
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_wait_until_ready_clears_stale_pre_connect_probe_manual_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
        circuit.mark_manual_required(
            reason="manual_gateway_api_recovery_required",
            source="market:pre_connect_api_probe",
            metadata={"gateway_reason": "gateway_api_half_open"},
        )
        manager = IBConnectionManager()
        manager._runtime_recovery_active = False
        manager.market_connected = lambda: True
        manager.exec_connected = lambda: True

        market_ready, exec_ready = await manager.wait_until_ready(
            host="127.0.0.1",
            port=4002,
            require_exec=True,
            require_master=False,
            timeout_sec=1.0,
            initiator="run_system.start",
        )

        assert market_ready is True
        assert exec_ready is True
        state = circuit.snapshot()
        assert state.open is False
        assert state.manual_required is False
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_operator_manual_circuit_still_blocks_real_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    ib_gateway_circuit._CIRCUIT = None
    try:
        circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
        circuit.mark_manual_required(
            reason="manual_gateway_api_recovery_required",
            source="operator:manual_proof",
            metadata={"gateway_reason": "gateway_api_half_open"},
        )
        manager = IBConnectionManager()

        with pytest.raises(ConnectionError, match="ib_gateway_connection_circuit_open"):
            await manager._ensure_recovery_short_circuit_allows_connect(
                caller="market",
                host="127.0.0.1",
                port=4002,
            )

        state = circuit.snapshot()
        assert state.open is True
        assert state.manual_required is True
    finally:
        ib_gateway_circuit._CIRCUIT = None


@pytest.mark.asyncio
async def test_manual_proof_requires_operator_ack_when_manual_required(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "operator:manual_proof")
    monkeypatch.delenv("IB_GATEWAY_RECOVERY_OPERATOR_ACK", raising=False)
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.mark_manual_required(reason="manual_gateway_api_recovery_required", source="market", metadata={})
    factory = MagicMock(side_effect=AssertionError("ack failure must not construct IB"))

    try:
        result = await run_authorized_gateway_recovery_proof(
            source="operator:manual_proof",
            ack_token=None,
            now=datetime.fromisoformat("2026-05-12T10:00:00+00:00"),
            ib_factory=factory,
        )

        assert result.status == "ACK_REQUIRED"
        assert result.ib_constructed is False
        factory.assert_not_called()
        assert circuit.snapshot().manual_required is True
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_manual_proof_unauthorized_source_does_not_construct_ib(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "operator:manual_proof")
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    ib_gateway_circuit.get_ib_gateway_connection_circuit().mark_manual_required(
        reason="manual_gateway_api_recovery_required",
        source="market",
        metadata={},
    )
    factory = MagicMock(side_effect=AssertionError("unauthorized proof must not construct IB"))

    try:
        result = await run_authorized_gateway_recovery_proof(
            source="external:manual_proof",
            ack_token="ACK-123",
            now=datetime.fromisoformat("2026-05-12T10:00:00+00:00"),
            ib_factory=factory,
        )

        assert result.status == "UNAUTHORIZED"
        assert result.ib_constructed is False
        factory.assert_not_called()
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_manual_proof_concurrent_callers_only_one_constructs_ib(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "operator:manual_proof")
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_PROOF_LEASE_SEC", "60")
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.mark_manual_required(reason="manual_gateway_api_recovery_required", source="market", metadata={})
    _FakeIBProofOK.constructed = 0

    try:
        first = await run_authorized_gateway_recovery_proof(
            source="operator:manual_proof",
            ack_token="ACK-123",
            now=datetime.fromisoformat("2026-05-12T10:00:00+00:00"),
            ib_factory=_FakeIBProofOK,
        )
        circuit.mark_manual_required(reason="manual_gateway_api_recovery_required", source="market", metadata={})
        circuit.try_acquire_manual_recovery_proof_lease(
            source="operator:manual_proof",
            lease_sec=60,
            now_ts=datetime.fromisoformat("2026-05-12T10:00:01+00:00").timestamp(),
        )
        second = await run_authorized_gateway_recovery_proof(
            source="operator:manual_proof",
            ack_token="ACK-123",
            now=datetime.fromisoformat("2026-05-12T10:00:02+00:00"),
            ib_factory=_FakeIBProofOK,
        )

        assert first.status == "PROOF_SUCCEEDED"
        assert second.status == "LEASE_DENIED"
        assert _FakeIBProofOK.constructed == 1
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


@pytest.mark.asyncio
async def test_manual_proof_failure_uses_report_aborted_not_report_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "operator:manual_proof")
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.mark_manual_required(reason="manual_gateway_api_recovery_required", source="market", metadata={})
    gatekeeper = MagicMock()

    try:
        result = await run_authorized_gateway_recovery_proof(
            source="operator:manual_proof",
            ack_token="ACK-123",
            now=datetime.fromisoformat("2026-05-12T10:00:00+00:00"),
            ib_factory=_FakeIBProofFailsPositions,
            gatekeeper=gatekeeper,
        )

        assert result.status == "PROOF_FAILED"
        gatekeeper.report_aborted.assert_called_once()
        gatekeeper.report_failure.assert_not_called()
        assert circuit.snapshot().manual_required is True
    finally:
        ib_gateway_circuit._CIRCUIT = None
        ib_gateway_recovery_coordinator._COORDINATOR = None


def test_offhours_disconnected_log_uses_separate_throttle_bucket(monkeypatch):
    manager = IBConnectionManager()
    manager._offhours_connectivity_log_cooldown_sec = 300.0
    monkeypatch.setattr(manager, "_is_rth_et", lambda **_kwargs: False)

    assert manager._should_emit_offhours_session_state_log(now_ts=1000.0, error_code=1100) is True
    assert manager._should_emit_offhours_disconnected_log(now_ts=1001.0) is True
    assert manager._should_emit_offhours_disconnected_log(now_ts=1100.0) is False
    assert manager._should_emit_offhours_disconnected_log(now_ts=1302.0) is True


def test_offhours_probe_fail_logs_are_throttled(monkeypatch):
    manager = IBConnectionManager()
    manager._offhours_probe_fail_log_cooldown_sec = 180.0
    monkeypatch.setattr(manager, "_is_rth_et", lambda **_kwargs: False)

    assert manager._should_emit_offhours_probe_failure_log(now_ts=2000.0) is True
    assert manager._should_emit_offhours_probe_failure_log(now_ts=2100.0) is False
    assert manager._should_emit_offhours_probe_failure_log(now_ts=2181.0) is True


def test_market_probe_interval_uses_offhours_floor(monkeypatch):
    manager = IBConnectionManager()
    manager._market_probe_interval_sec = 30.0
    manager._market_probe_interval_offhours_sec = 120.0
    monkeypatch.setattr(manager, "_is_rth_et", lambda **_kwargs: False)
    assert manager._market_probe_sleep_interval_sec(now_ts=1000.0) == 120.0

    monkeypatch.setattr(manager, "_is_rth_et", lambda **_kwargs: True)
    assert manager._market_probe_sleep_interval_sec(now_ts=1000.0) == 30.0


def test_session_phase_treats_4am_as_premarket():
    manager = IBConnectionManager()
    ts = datetime(2026, 4, 8, 4, 30, tzinfo=ET).timestamp()
    assert manager._session_phase_et(now_ts=ts) == "PREMARKET"


@pytest.mark.asyncio
async def test_market_shutdown_resets_event_binding(monkeypatch):
    manager = IBConnectionManager()

    class _FakeIB:
        def __init__(self):
            self.connected = True

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    manager._market_ib = _FakeIB()
    manager._market_events_bound = True

    await manager.shutdown()

    assert manager._market_ib is None
    assert manager._market_events_bound is False


def test_option_reject_is_recorded_without_warning(monkeypatch):
    manager = IBConnectionManager()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    manager._handle_market_error_event(57, 354, "Not subscribed", _DummyContract())

    assert manager.was_market_req_rejected(57, error_codes=(354,)) is True
    assert mock_logger.warning.call_count == 0
    assert mock_logger.info.call_count >= 1


def test_vix_index_354_is_logged_as_info(monkeypatch):
    manager = IBConnectionManager()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    manager._handle_market_error_event(14, 354, "Requested market data is not subscribed", _DummyVixIndexContract())

    assert mock_logger.warning.call_count == 0
    assert mock_logger.info.call_count >= 1


def test_unresolved_contract_354_is_logged_as_info(monkeypatch):
    manager = IBConnectionManager()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    manager._handle_market_error_event(102, 354, "Requested market data is not subscribed", None)

    assert manager.was_market_req_rejected(102, error_codes=(354,)) is True
    assert mock_logger.warning.call_count == 0
    assert mock_logger.info.call_count >= 1


def test_cancel_after_reject_300_is_logged_as_info(monkeypatch):
    manager = IBConnectionManager()
    mock_logger = MagicMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.logger", mock_logger)

    manager._handle_market_error_event(102, 354, "Requested market data is not subscribed", None)
    manager._handle_market_error_event(102, 300, "Can't find EId with tickerId:102", None)

    assert mock_logger.warning.call_count == 0
    assert mock_logger.info.call_count >= 2


class _FakeIBTimeoutThenReady:
    def __init__(self):
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connectAsync(self, host, port, clientId, timeout):
        self.connect_calls += 1
        self.connected = True
        if self.connect_calls == 1:
            raise TimeoutError("apiStart timeout")

    async def reqCurrentTimeAsync(self):
        if not self.connected:
            raise ConnectionError("not connected")
        return "ok"

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False
        self.disconnect_calls += 1


class _FakeIBAlwaysTimeout:
    def __init__(self):
        self.connected = False
        self.disconnect_calls = 0

    async def connectAsync(self, host, port, clientId, timeout):
        self.connected = True
        raise TimeoutError("apiStart timeout")

    async def reqCurrentTimeAsync(self):
        raise TimeoutError("probe timeout")

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False
        self.disconnect_calls += 1


class _FakeIBTimeoutButReportsDisconnected:
    def __init__(self):
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connectAsync(self, host, port, clientId, timeout):
        self.connect_calls += 1
        raise TimeoutError("apiStart timeout")

    async def reqCurrentTimeAsync(self):
        raise TimeoutError("probe timeout")

    def isConnected(self):
        return False

    def disconnect(self):
        self.disconnect_calls += 1


class _FakeIBProbeOK:
    def __init__(self):
        self.connected = True

    def isConnected(self):
        return self.connected

    async def reqCurrentTimeAsync(self):
        return "ok"


class _FakeIBProbeConnectOK:
    instances = []

    def __init__(self):
        self.connected = False
        self.disconnect_calls = 0
        self.connect_calls = 0
        self.client_ids = []
        self.instances.append(self)

    async def connectAsync(self, host, port, clientId, timeout):
        self.connect_calls += 1
        self.client_ids.append(clientId)
        self.connected = True

    async def reqCurrentTimeAsync(self):
        if not self.connected:
            raise ConnectionError("not connected")
        return "ok"

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False
        self.disconnect_calls += 1


class _FakeIBProofOK(_FakeIBProbeConnectOK):
    constructed = 0

    def __init__(self):
        type(self).constructed += 1
        super().__init__()
        self.open_order_refresh_calls = 0

    def positions(self):
        return []

    async def reqOpenOrdersAsync(self):
        self.open_order_refresh_calls += 1
        return []

    def openTrades(self):
        return []


class _FakeIBProofFailsPositions(_FakeIBProbeConnectOK):
    def positions(self):
        raise RuntimeError("positions unavailable")

    async def reqOpenOrdersAsync(self):
        return []

    def openTrades(self):
        return []


class _FakeIBProbeClientIdInUse:
    instances = []

    def __init__(self):
        self.disconnect_calls = 0
        self.client_ids = []
        self.instances.append(self)

    async def connectAsync(self, host, port, clientId, timeout):
        self.client_ids.append(clientId)
        raise ConnectionError("client id is already in use")

    async def reqCurrentTimeAsync(self):
        raise AssertionError("reqCurrentTimeAsync must not run after connect failure")

    def isConnected(self):
        return False

    def disconnect(self):
        self.disconnect_calls += 1


class _FakeIBWithMarketDataType:
    def __init__(self):
        self.calls = []

    def reqMarketDataType(self, market_data_type):
        self.calls.append(market_data_type)


@pytest.mark.asyncio
async def test_connect_retry_disconnects_partial_socket_before_retry():
    fake_ib = _FakeIBTimeoutThenReady()

    connected_ib, used = await _connect_ib_with_retry(fake_ib, "127.0.0.1", 4002, 123)

    assert used == 123
    assert connected_ib is fake_ib
    assert fake_ib.connect_calls == 2
    assert fake_ib.disconnect_calls == 1
    assert fake_ib.isConnected() is True


@pytest.mark.asyncio
async def test_connect_retry_disconnects_even_when_ib_reports_disconnected():
    fake_ib = _FakeIBTimeoutButReportsDisconnected()

    with pytest.raises(TimeoutError):
        await _connect_ib_with_retry(fake_ib, "127.0.0.1", 4002, 123)

    assert fake_ib.connect_calls == 5
    assert fake_ib.disconnect_calls == 5


def test_market_data_type_for_now_defaults_to_live_in_rth(monkeypatch):
    monkeypatch.delenv("IB_MARKET_DATA_TYPE_RTH", raising=False)
    assert _market_data_type_for_now(datetime(2026, 3, 25, 10, 0, tzinfo=ET)) == 1


def test_market_data_type_for_now_defaults_to_delayed_frozen_offhours(monkeypatch):
    monkeypatch.delenv("IB_MARKET_DATA_TYPE_OFFHOURS", raising=False)
    assert _market_data_type_for_now(datetime(2026, 3, 25, 18, 0, tzinfo=ET)) == 4


def test_market_data_type_for_now_honors_env_override(monkeypatch):
    monkeypatch.setenv("IB_MARKET_DATA_TYPE_OFFHOURS", "2")
    assert _market_data_type_for_now(datetime(2026, 3, 25, 18, 0, tzinfo=ET)) == 2


def test_set_delayed_frozen_market_data_uses_session_appropriate_type(monkeypatch):
    fake_ib = _FakeIBWithMarketDataType()
    monkeypatch.setattr("services.ibkr.ib_connection_manager._market_data_type_for_now", lambda now=None: 1)

    _try_set_delayed_frozen_market_data(fake_ib)

    assert fake_ib.calls == [1]


@pytest.mark.asyncio
async def test_get_exec_adapter_reuses_existing_global_adapter(monkeypatch):
    manager = IBConnectionManager()
    manager._probe_api_handshake_ready = AsyncMock(return_value=True)
    shared = MagicMock()
    shared.client_id = 207
    shared.account = ""
    shared.host = "127.0.0.1"
    shared.port = 4002
    shared.start = AsyncMock(return_value=None)
    shared.ib = MagicMock()
    shared.ib.isConnected.return_value = True

    monkeypatch.setattr(
        "services.exec.adapters.ib_adapter.peek_global_ib_adapter",
        lambda: shared,
    )

    adapter = await manager.get_exec_adapter(host="127.0.0.1", port=4002, account="DU123")

    assert adapter is shared
    assert manager._exec_adapter is shared
    assert shared.host == "127.0.0.1"
    assert shared.port == 4002
    assert shared.account == "DU123"
    shared.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_exec_adapter_raises_when_start_returns_degraded(monkeypatch):
    manager = IBConnectionManager()
    manager._probe_api_handshake_ready = AsyncMock(return_value=True)
    shared = MagicMock()
    shared.client_id = 207
    shared.account = ""
    shared.host = "127.0.0.1"
    shared.port = 4002
    shared.start = AsyncMock(return_value=None)
    shared.ib = MagicMock()
    shared.ib.isConnected.return_value = False

    monkeypatch.setattr(
        "services.exec.adapters.ib_adapter.peek_global_ib_adapter",
        lambda: shared,
    )

    with pytest.raises(ConnectionError, match="ib_exec_not_connected"):
        await manager.get_exec_adapter(host="127.0.0.1", port=4002, account="DU123")


@pytest.mark.asyncio
async def test_exec_epoch_advances_only_on_new_connect_generation():
    manager = IBConnectionManager()

    assert manager.exec_connection_epoch() == 0
    await manager._on_exec_adapter_connected({"generation": 1, "client_id": 20, "source": "connect"})
    assert manager.exec_connection_epoch() == 1
    await manager._on_exec_adapter_connected({"generation": 1, "client_id": 20, "source": "connect"})
    assert manager.exec_connection_epoch() == 1
    await manager._on_exec_adapter_connected({"generation": 2, "client_id": 20, "source": "connect"})
    assert manager.exec_connection_epoch() == 2


@pytest.mark.asyncio
async def test_get_exec_ib_handle_reflects_new_epoch_after_reconnect_callback():
    manager = IBConnectionManager()
    adapter = MagicMock()
    adapter.client_id = 20
    adapter.ib = MagicMock()
    adapter.ib.isConnected.return_value = True
    manager._exec_adapter = adapter

    await manager._on_exec_adapter_connected({"generation": 1, "client_id": 20, "source": "connect"})
    h1 = await manager.get_exec_ib_handle(host="127.0.0.1", port=4002, account="DU123")
    await manager._on_exec_adapter_connected({"generation": 2, "client_id": 20, "source": "connect"})
    h2 = await manager.get_exec_ib_handle(host="127.0.0.1", port=4002, account="DU123")

    assert h1.client_id == 20
    assert h2.client_id == 20
    assert h2.epoch == h1.epoch + 1


def test_default_exec_client_id_uses_stable_offset(monkeypatch):
    monkeypatch.delenv("IB_EXEC_CLIENT_ID", raising=False)
    assert _default_exec_client_id() == int(get_client_id_registry().resolve("exec"))


def test_default_exec_client_id_honors_env_override(monkeypatch):
    monkeypatch.setenv("IB_EXEC_CLIENT_ID", "1259")
    assert _default_exec_client_id() == 1259


def test_default_market_client_id_uses_stable_offset(monkeypatch):
    monkeypatch.delenv("IB_MARKET_CLIENT_ID", raising=False)
    assert _default_market_client_id() == int(get_client_id_registry().resolve("market"))


def test_default_market_client_id_honors_env_override(monkeypatch):
    monkeypatch.setenv("IB_MARKET_CLIENT_ID", "1337")
    assert _default_market_client_id() == 1337


@pytest.mark.asyncio
async def test_connect_retry_raises_after_exhausted_timeouts_without_false_success():
    fake_ib = _FakeIBAlwaysTimeout()

    with pytest.raises(TimeoutError):
        await _connect_ib_with_retry(fake_ib, "127.0.0.1", 4002, 123)

    assert fake_ib.disconnect_calls == 5


@pytest.mark.asyncio
async def test_connect_retry_recreates_ib_client_after_timeout(monkeypatch):
    created = []
    fallback_counts = []

    class _FactoryIB:
        def __init__(self, fail_once: bool):
            self.fail_once = fail_once
            self.connected = False
            self.connect_calls = 0
            self.disconnect_calls = 0

        async def connectAsync(self, host, port, clientId, timeout):
            self.connect_calls += 1
            self.connected = True
            if self.fail_once:
                self.fail_once = False
                raise TimeoutError("apiStart timeout")

        async def reqCurrentTimeAsync(self):
            if not self.connected:
                raise ConnectionError("not connected")
            return "ok"

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False
            self.disconnect_calls += 1

    def _factory():
        inst = _FactoryIB(fail_once=False)
        created.append(inst)
        return inst

    first = _FactoryIB(fail_once=True)
    connected_ib, used = await _connect_ib_with_retry(first, "127.0.0.1", 4002, 123, ib_factory=_factory)

    assert used == 123
    assert connected_ib is created[0]
    assert first.disconnect_calls == 1
    assert connected_ib.isConnected() is True


@pytest.mark.asyncio
async def test_connect_retry_keeps_client_id_after_timeout():
    created = []

    class _FactoryIB:
        def __init__(self, timeout_for_client_id: Optional[int] = None):
            self.timeout_for_client_id = timeout_for_client_id
            self.connected = False
            self.disconnect_calls = 0
            self.client_ids = []

        async def connectAsync(self, host, port, clientId, timeout):
            self.client_ids.append(clientId)
            self.connected = True
            if clientId == self.timeout_for_client_id:
                raise TimeoutError("apiStart timeout")

        async def reqCurrentTimeAsync(self):
            if not self.connected:
                raise ConnectionError("not connected")
            return "ok"

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False
            self.disconnect_calls += 1

    def _factory():
        inst = _FactoryIB()
        created.append(inst)
        return inst

    first = _FactoryIB(timeout_for_client_id=123)
    connected_ib, used = await _connect_ib_with_retry(
        first,
        "127.0.0.1",
        4002,
        123,
        ib_factory=_factory,
    )

    assert used == 123
    assert first.disconnect_calls == 1
    assert connected_ib is created[0]
    assert connected_ib.client_ids == [123]


@pytest.mark.asyncio
async def test_connect_retry_can_switch_client_id_after_timeout_when_enabled():
    created = []

    class _FactoryIB:
        def __init__(self, timeout_for_client_id: Optional[int] = None):
            self.timeout_for_client_id = timeout_for_client_id
            self.connected = False
            self.client_ids = []

        async def connectAsync(self, host, port, clientId, timeout):
            self.client_ids.append(clientId)
            self.connected = True
            if clientId == self.timeout_for_client_id:
                raise TimeoutError("apiStart timeout")

        async def reqCurrentTimeAsync(self):
            return "ok"

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    def _factory():
        inst = _FactoryIB()
        created.append(inst)
        return inst

    first = _FactoryIB(timeout_for_client_id=123)
    connected_ib, used = await _connect_ib_with_retry(
        first,
        "127.0.0.1",
        4002,
        123,
        ib_factory=_factory,
        timeout_client_id_fallbacks=2,
    )

    assert used == 124
    assert connected_ib is created[0]
    assert connected_ib.client_ids == [124]


@pytest.mark.asyncio
async def test_market_client_id_kept_across_leases_after_timeout(monkeypatch):
    manager = IBConnectionManager()
    manager._gatekeeper.acquire = AsyncMock(return_value=True)
    manager._gatekeeper.report_failure = MagicMock()
    manager._gatekeeper.report_success = MagicMock()
    manager._probe_api_handshake_ready = AsyncMock(return_value=True)

    created = []
    fallback_counts = []

    class _ConnectedIB:
        def __init__(self, client_id: int):
            self._client_id = client_id
            self.client = MagicMock(clientId=client_id)
            self.connected = True

        def isConnected(self):
            return self.connected

        async def reqCurrentTimeAsync(self):
            return "ok"

    async def _fake_connect(ib, host, port, client_id, **kwargs):
        created.append(client_id)
        fallback_counts.append(kwargs.get("timeout_client_id_fallbacks"))
        if len(created) == 1:
            raise TimeoutError("apiStart timeout")
        return _ConnectedIB(client_id), client_id

    monkeypatch.setattr("services.ibkr.ib_connection_manager._connect_ib_with_retry", _fake_connect)
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: MagicMock())
    monkeypatch.setattr("services.ibkr.ib_connection_manager._try_set_delayed_frozen_market_data", lambda ib: None)
    monkeypatch.setattr(manager, "_bind_market_events", lambda: None)
    monkeypatch.setattr(manager, "_probe_market_connection", AsyncMock())
    monkeypatch.setattr(manager, "_ensure_market_probe_task", lambda: None)

    with pytest.raises(TimeoutError):
        await manager.get_market_ib(host="127.0.0.1", port=4002)

    assert manager._market_connect_timeout_streak == 1

    connected = await manager.get_market_ib(host="127.0.0.1", port=4002)

    assert created == [_default_market_client_id(), _default_market_client_id()]
    assert fallback_counts == [2, 2]
    assert manager._market_client_id == _default_market_client_id()
    assert manager._market_connect_timeout_streak == 0
    assert connected.isConnected() is True


@pytest.mark.asyncio
async def test_market_probe_updates_health_snapshot(monkeypatch):
    manager = IBConnectionManager()
    manager._market_ib = _FakeIBProbeOK()
    monkeypatch.setattr("services.ibkr.ib_connection_manager.set_ibkr_connection_up", lambda *args, **kwargs: None)
    monkeypatch.setattr("services.ibkr.ib_connection_manager.mark_ibkr_probe_ok", lambda *args, **kwargs: None)

    await manager._probe_market_connection(source="test")

    snap = manager.get_market_health_snapshot()
    assert snap["last_probe_ok_ts"] is not None
    assert snap["last_probe_latency_ms"] is not None


@pytest.mark.asyncio
async def test_market_cancelled_connect_releases_gatekeeper_slot(monkeypatch):
    manager = IBConnectionManager()
    manager._probe_api_handshake_ready = AsyncMock(return_value=True)
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: MagicMock())

    async def _cancelled_connect(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr("services.ibkr.ib_connection_manager._connect_ib_with_retry", _cancelled_connect)

    with pytest.raises(asyncio.CancelledError):
        await manager.get_market_ib(host="127.0.0.1", port=4002)

    assert manager._gatekeeper._attempt_slot.locked() is False
    assert manager._gatekeeper._attempt_owner is None


@pytest.mark.asyncio
async def test_master_gatekeeper_denied_does_not_connect(monkeypatch):
    manager = IBConnectionManager()
    manager._gatekeeper.acquire = AsyncMock(return_value=False)
    manager._gatekeeper.report_failure = MagicMock()
    manager._gatekeeper.report_success = MagicMock()
    manager._probe_api_handshake_ready = AsyncMock(return_value=True)
    connect = AsyncMock()
    monkeypatch.setattr("services.ibkr.ib_connection_manager._connect_ib_with_retry", connect)

    with pytest.raises(ConnectionError, match="reconnect_gatekeeper_denied:master"):
        await manager.get_master_ib(host="127.0.0.1", port=4002)

    manager._gatekeeper.acquire.assert_awaited_once()
    connect.assert_not_awaited()
    manager._gatekeeper.report_failure.assert_not_called()
    manager._gatekeeper.report_success.assert_not_called()


@pytest.mark.asyncio
async def test_master_connect_uses_gatekeeper_and_reports_success(monkeypatch):
    manager = IBConnectionManager()
    manager._gatekeeper.acquire = AsyncMock(return_value=True)
    manager._gatekeeper.report_failure = MagicMock()
    manager._gatekeeper.report_success = MagicMock()
    manager._gatekeeper.report_aborted = MagicMock()
    manager._probe_api_handshake_ready = AsyncMock(return_value=True)

    connected_ib = MagicMock()
    connected_ib.isConnected.return_value = True
    fallback_counts = []

    async def _fake_connect(ib, host, port, client_id, **kwargs):
        fallback_counts.append(kwargs.get("timeout_client_id_fallbacks"))
        return connected_ib, client_id

    monkeypatch.setattr("services.ibkr.ib_connection_manager._connect_ib_with_retry", _fake_connect)
    monkeypatch.setattr("services.ibkr.ib_connection_manager.IB", lambda: MagicMock())
    monkeypatch.setattr(manager, "_bind_master_events", lambda: None)

    out = await manager.get_master_ib(host="127.0.0.1", port=4002)

    assert out is connected_ib
    manager._gatekeeper.acquire.assert_awaited_once()
    manager._gatekeeper.report_success.assert_called_once_with(caller="master")
    manager._gatekeeper.report_failure.assert_not_called()
    manager._gatekeeper.report_aborted.assert_not_called()
    assert fallback_counts == [2]
    assert manager._master_client_id == int(get_client_id_registry().resolve("master"))
    assert manager._master_connection_epoch == 1


def test_stale_market_disconnect_event_does_not_invalidate_active_session():
    manager = IBConnectionManager()

    stale_ib = MagicMock()
    active_ib = MagicMock()
    active_ib.isConnected.return_value = True

    manager._market_ib = active_ib
    manager._market_client_id = 27
    manager._handle_market_disconnected(source_ib=stale_ib)

    assert manager._market_ib is active_ib
    assert manager._market_client_id == 27
