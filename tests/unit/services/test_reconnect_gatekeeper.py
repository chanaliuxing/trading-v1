"""Tests for IBReconnectGatekeeper."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from services.ibkr import ib_gateway_circuit
from services.ibkr.ib_connection_manager import IBReconnectGatekeeper


@pytest.fixture
def gk():
    return IBReconnectGatekeeper()


@pytest.fixture(autouse=True)
def _reset_gateway_circuit_singleton():
    ib_gateway_circuit._CIRCUIT = None
    try:
        yield
    finally:
        ib_gateway_circuit._CIRCUIT = None


class TestTcpProbe:
    @pytest.mark.asyncio
    async def test_probe_returns_true_when_port_open(self, gk):
        async def _fake_open(*a, **kw):
            writer = AsyncMock()
            writer.close = lambda: None
            writer.wait_closed = AsyncMock()
            return AsyncMock(), writer

        with patch("asyncio.open_connection", side_effect=_fake_open):
            assert await gk.tcp_probe("127.0.0.1", 4002) is True

    @pytest.mark.asyncio
    async def test_probe_returns_false_on_connection_refused(self, gk):
        with patch(
            "asyncio.open_connection",
            side_effect=ConnectionRefusedError,
        ):
            assert await gk.tcp_probe("127.0.0.1", 4002) is False

    @pytest.mark.asyncio
    async def test_probe_returns_false_on_timeout(self, gk):
        with patch(
            "asyncio.open_connection",
            side_effect=asyncio.TimeoutError,
        ):
            assert await gk.tcp_probe("127.0.0.1", 4002) is False


class TestBackoffComputation:
    def test_initial_backoff_is_base(self, gk):
        assert gk._consecutive_failures == 0
        assert gk._readiness_failures == 0
        with patch(
            "services.ibkr.ib_connection_manager._session_phase_et_static",
            return_value="RTH",
        ):
            assert gk._compute_backoff() == 2.0

    def test_rth_backoff_caps_at_15(self, gk):
        gk._consecutive_failures = 10
        with patch(
            "services.ibkr.ib_connection_manager._session_phase_et_static",
            return_value="RTH",
        ):
            assert gk._compute_backoff() == 15.0

    def test_premarket_backoff_caps_at_24(self, gk):
        gk._consecutive_failures = 10
        with patch(
            "services.ibkr.ib_connection_manager._session_phase_et_static",
            return_value="PREMARKET",
        ):
            # 3 * 2^min(10,3) = 3 * 8 = 24, capped by min(30, 24) = 24
            assert gk._compute_backoff() == 24.0

    def test_offhours_backoff_caps_at_80(self, gk):
        gk._consecutive_failures = 20
        with patch(
            "services.ibkr.ib_connection_manager._session_phase_et_static",
            return_value="OFFHOURS",
        ):
            # 5 * 2^min(20,4) = 5 * 16 = 80, capped by min(120, 80) = 80
            assert gk._compute_backoff() == 80.0

    def test_offhours_exponential_sequence(self, gk):
        with patch(
            "services.ibkr.ib_connection_manager._session_phase_et_static",
            return_value="OFFHOURS",
        ):
            expected = [5.0, 10.0, 20.0, 40.0, 80.0]
            for i, exp in enumerate(expected):
                gk._consecutive_failures = i
                assert gk._compute_backoff() == exp

    def test_backoff_uses_shared_readiness_failures(self, gk):
        gk._readiness_failures = 3
        with patch(
            "services.ibkr.ib_connection_manager._session_phase_et_static",
            return_value="RTH",
        ):
            assert gk._compute_backoff() == 15.0


class TestRateLimit:
    def test_allows_within_window(self, gk):
        gk._max_attempts_per_window = 3
        gk._window_sec = 10.0
        assert gk._check_rate_limit() is True
        assert gk._check_rate_limit() is True
        assert gk._check_rate_limit() is True

    def test_denies_over_limit(self, gk):
        gk._max_attempts_per_window = 2
        gk._window_sec = 10.0
        assert gk._check_rate_limit() is True
        assert gk._check_rate_limit() is True
        assert gk._check_rate_limit() is False

    def test_window_expires(self, gk):
        gk._max_attempts_per_window = 1
        gk._window_sec = 0.01
        assert gk._check_rate_limit() is True
        time.sleep(0.02)
        assert gk._check_rate_limit() is True


class TestAcquire:
    @pytest.mark.asyncio
    async def test_acquire_succeeds_without_tcp_probe(self, gk):
        result = await gk.acquire(caller="test", host="127.0.0.1", port=4002)
        assert result is True

    @pytest.mark.asyncio
    async def test_acquire_does_not_run_tcp_probe(self, gk):
        with patch.object(gk, "tcp_probe", side_effect=AssertionError("acquire should not probe")):
            result = await gk.acquire(caller="test", host="127.0.0.1", port=4002)
            assert result is True
            assert gk._consecutive_failures == 0
            assert gk._readiness_failures == 0

    @pytest.mark.asyncio
    async def test_acquire_rate_limited(self, gk):
        gk._max_attempts_per_window = 1
        gk._window_sec = 60.0
        assert await gk.acquire(caller="a", host="h", port=1) is True
        assert await gk.acquire(caller="b", host="h", port=1) is False

    @pytest.mark.asyncio
    async def test_probe_ready_does_not_consume_attempt_budget(self, gk):
        gk._max_attempts_per_window = 1
        gk._window_sec = 60.0
        with patch.object(gk, "tcp_probe", return_value=True):
            assert await gk.probe_ready(caller="probe", host="h", port=1) is True
            assert gk._attempt_timestamps == []
            assert await gk.acquire(caller="connect", host="h", port=1) is True

    @pytest.mark.asyncio
    async def test_probe_ready_failure_increments_shared_readiness_backoff(self, gk):
        with patch.object(gk, "tcp_probe", return_value=False):
            assert await gk.probe_ready(caller="probe", host="h", port=1) is False
            assert gk._readiness_failures == 1
            assert gk._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_probe_ready_success_resets_readiness_failures(self, gk):
        gk._readiness_failures = 3
        with patch.object(gk, "tcp_probe", return_value=True):
            assert await gk.probe_ready(caller="probe", host="h", port=1) is True
            assert gk._readiness_failures == 0


class TestReportSuccessFailure:
    def test_report_success_resets_state(self, gk):
        gk._consecutive_failures = 5
        gk._readiness_failures = 2
        gk._attempt_timestamps = [time.time()] * 3
        gk.report_success(caller="test")
        assert gk._consecutive_failures == 0
        assert gk._readiness_failures == 0
        assert gk._attempt_timestamps == []
        assert gk._last_success_ts > 0

    def test_report_failure_increments(self, gk):
        gk._readiness_failures = 2
        gk.report_failure(caller="test", error="conn refused")
        assert gk._consecutive_failures == 1
        assert gk._readiness_failures == 0
        gk.report_failure(caller="test", error="conn refused")
        assert gk._consecutive_failures == 2


class TestGatewayCircuit:
    @pytest.mark.asyncio
    async def test_gateway_circuit_blocks_acquire_after_api_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
        monkeypatch.setenv("IB_GATEWAY_CIRCUIT_BASE_COOLDOWN_SEC", "30")
        monkeypatch.setenv("IB_GATEWAY_CIRCUIT_MAX_COOLDOWN_SEC", "60")
        ib_gateway_circuit._CIRCUIT = None

        gk = IBReconnectGatekeeper()
        gk.report_failure(
            caller="market",
            error="ConnectionRefusedError: [Errno 61] Connect call failed ('127.0.0.1', 4002)",
            host="127.0.0.1",
            port=4002,
        )

        state = gk.gateway_circuit_snapshot()
        assert state["open"] is True
        assert state["retry_after_sec"] > 0
        assert await gk.acquire(caller="exec", host="127.0.0.1", port=4002) is False

    @pytest.mark.asyncio
    async def test_gateway_circuit_success_clears_persistent_block(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
        monkeypatch.setenv("IB_GATEWAY_CIRCUIT_BASE_COOLDOWN_SEC", "30")
        ib_gateway_circuit._CIRCUIT = None

        gk = IBReconnectGatekeeper()
        gk.report_failure(
            caller="market",
            error="TimeoutError: ib_api_handshake_not_ready",
            host="127.0.0.1",
            port=4002,
        )
        assert gk.gateway_circuit_snapshot()["open"] is True

        gk.report_success(caller="market")

        assert gk.gateway_circuit_snapshot()["open"] is False

    def test_gateway_circuit_allows_single_recovery_probe_lease(self, tmp_path, monkeypatch):
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

        first = circuit.try_acquire_recovery_probe_lease(
            source="manager_runtime_recovery:circuit_recovery_probe",
            min_interval_sec=10.0,
            lease_sec=5.0,
            now_ts=1001.0,
        )
        second = circuit.try_acquire_recovery_probe_lease(
            source="run_system.start:circuit_recovery_probe",
            min_interval_sec=10.0,
            lease_sec=5.0,
            now_ts=1002.0,
        )

        assert first["allowed"] is True
        assert first["reason"] == "recovery_probe_lease_acquired"
        assert second["allowed"] is False
        assert second["reason"] == "recovery_probe_lease_active"
        assert second["retry_after_sec"] == pytest.approx(4.0)

    def test_gateway_circuit_recovery_probe_lease_respects_manual_required(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
        ib_gateway_circuit._CIRCUIT = None

        circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
        circuit.mark_manual_required(
            reason="manual_gateway_api_recovery_required",
            source="market",
            metadata={"host": "127.0.0.1", "port": 4002},
        )

        decision = circuit.try_acquire_recovery_probe_lease(
            source="manager_runtime_recovery:circuit_recovery_probe",
            min_interval_sec=10.0,
            lease_sec=5.0,
            now_ts=1001.0,
        )

        assert decision["allowed"] is False
        assert decision["reason"] == "manual_required"


class TestSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_acquires_are_serialized(self, gk):
        order = []

        async def _acquire(name, delay):
            await gk.acquire(caller=name, host="h", port=1)
            order.append(name)
            await asyncio.sleep(delay)

        gk._max_attempts_per_window = 10
        t1 = asyncio.create_task(_acquire("first", 0.05))
        t2 = asyncio.create_task(_acquire("second", 0.0))
        await asyncio.sleep(0.01)
        await asyncio.gather(t1, t2)
        assert order[0] == "first"

    @pytest.mark.asyncio
    async def test_probe_ready_is_serialized_with_other_probes(self, gk):
        order = []

        async def _probe(name, delay):
            async def _fake_probe(*_args, **_kwargs):
                await asyncio.sleep(delay)
                return True

            with patch.object(gk, "tcp_probe", side_effect=_fake_probe):
                await gk.probe_ready(caller=name, host="h", port=1)
                order.append(name)

        t1 = asyncio.create_task(_probe("first", 0.05))
        t2 = asyncio.create_task(_probe("second", 0.0))
        await asyncio.sleep(0.01)
        await asyncio.gather(t1, t2)
        assert order == ["first", "second"]
